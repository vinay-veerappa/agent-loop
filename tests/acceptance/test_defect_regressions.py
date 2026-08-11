"""
Regression tests for the defects found in the 2026-08-10 review.

Every test here failed before its fix. They are grouped by the defect they
pin, and each names the failure mode in its docstring -- these are the cases
the harness's own acceptance suite passed straight through, because it tested
the state machine against fakes and never crossed a boundary: a real pytest
summary, a real `git stash`, a real second language, a real promote.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import (
    arbiter, compaction, context, gates, memory, models, providers, regions, workspace,
)
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion


PY = Profile(
    name="test-regressions-py",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PY)

CS = Profile(
    name="test-regressions-cs",
    language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=("/*", "*/"), block_kind="decl", ascii_only=True,
    implementer_rules="t", reviewer_priorities="t",
)
register(CS)


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


# ---------------------------------------------------------------------------
# gates.parse_tests -- an ordinary green pytest run must not read as "aborted"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "summary,passed,failed,ran",
    [
        ("===== 17 passed in 2.31s =====", 17, 0, True),
        ("===== 17 passed, 1 warning in 2.31s =====", 17, 0, True),
        ("===== 15 passed, 2 skipped in 1.02s =====", 15, 0, True),
        ("===== 1 failed, 16 passed in 2.31s =====", 16, 1, True),
        ("===== 1 failed, 16 passed, 3 warnings in 2.31s =====", 16, 1, True),
        ("===== 12 passed, 1 xfailed, 2 warnings in 3.0s =====", 12, 0, True),
        ("===== no tests ran in 0.01s =====", 0, 0, False),
    ],
)
def test_pytest_summary_variants(summary, passed, failed, ran):
    """A warning or a skip in the summary line used to make the whole run
    unparseable, which failed the gate and made capture_baseline refuse."""
    out = gates.parse_tests(summary)
    assert out.ran is ran, f"{summary!r} -> ran={out.ran}, want {ran}"
    assert out.passed == passed
    assert out.failed == failed


def test_pytest_collection_errors_are_not_a_baseline():
    """A suite that errored during collection reported no verdicts, so it must
    not be frozen as the expected-failure baseline."""
    out = gates.parse_tests(
        "ERROR scripts/tests/test_x.py - FileNotFoundError\n"
        "==== 1 warning, 15 errors in 2.10s ===="
    )
    assert out.errors == 15
    assert out.ran is False, "an errored collection did not run the tests"


def test_nt8_results_format_still_parses():
    out = gates.parse_tests("[FAIL] MyTest.Foo\nRESULTS: Passed = 30, Failed = 1")
    assert (out.ran, out.passed, out.failed) == (True, 30, 1)
    assert out.failures == {"MyTest.Foo"}


def test_baseline_refuses_unparseable_and_errored_runs(tmp_path):
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "BASE") as ws:
        with pytest.raises(workspace.WorkspaceError, match="no parseable result summary"):
            workspace.capture_baseline(ws, "python -c \"print('nothing useful')\"", gates.parse_tests)
        with pytest.raises(workspace.WorkspaceError, match="suite-level error"):
            workspace.capture_baseline(
                ws, "python -c \"print('==== 2 errors in 0.1s ====')\"", gates.parse_tests
            )


# ---------------------------------------------------------------------------
# gates -- the compile gate must see the patch; expect_green must not be fuzzy
# ---------------------------------------------------------------------------
def test_compile_gate_substitutes_touched_files(tmp_path):
    """A build_cmd naming a fixed target passes no matter what the patch did.
    {files} makes the gate look at the files that actually changed."""
    repo = _git_repo(tmp_path)
    (repo / "src" / "broken.py").write_text("def f(:\n", encoding="utf-8")

    ok = gates.check_compile("python -m py_compile {files}", repo, files=["src/target.py"])
    assert ok.ok, ok.detail

    bad = gates.check_compile("python -m py_compile {files}", repo, files=["src/broken.py"])
    assert not bad.ok, "the gate must fail on the file the patch actually touched"

    empty = gates.check_compile("python -m py_compile {files}", repo, files=[])
    assert empty.ok and "no files" in empty.summary


def test_expect_green_requires_a_whole_identifier():
    """Substring matching let `test_foo` be satisfied by `test_foo_bar`, so a
    misspelled expect_green entry silently passed the test-first check."""
    assert gates.names_match("test_foo", "tests/t.py::test_foo")
    assert gates.names_match("test_foo", "[FAIL] Suite.test_foo")
    assert not gates.names_match("test_foo", "tests/t.py::test_foo_bar")
    assert not gates.names_match("test_foo", "tests/t.py::xtest_foo")


def test_static_gate_is_language_aware():
    """Brace counting and ASCII-only are C# rules, not universal ones. A Python
    dict literal is not an unbalanced block, and a non-ASCII string is fine."""
    reg = [regions.Region("R", "a.py", Path("a.py"), "def f", "indent", 0, 1, "def f():\n    pass")]
    body = {"R": 'def f():\n    return {"k": "é"}'}
    res = gates.check_static(reg, body, lambda ln: regions.strip_code(ln, PY), PY)
    assert res.ok, res.detail


# ---------------------------------------------------------------------------
# compaction -- the ticket and the candidate under revision must survive
# ---------------------------------------------------------------------------
def _history(prompt: str, rounds: int, size: int = 8000):
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": prompt}]
    for i in range(rounds):
        h.append({"role": "assistant", "content": f"BLOCK{i} " + "x" * size})
        h.append({"role": "user", "content": f"- [BLOCKER] finding {i}\n" + "y" * size})
    return h


def test_compaction_keeps_the_implement_prompt():
    """Phase 4a folded the implement prompt -- the ticket, the spec and the
    verbatim region source -- into a 1000-char stub, then told the implementer
    to "re-emit ALL blocks in full"."""
    prompt = "# TICKET T1\n## Regions to rewrite\n" + "REGION SOURCE LINE\n" * 500
    out = compaction.compact_history(_history(prompt, 2), 3, PY)
    assert out[1]["content"] == prompt, "the implement prompt must be preserved verbatim"


def test_compaction_survives_phase4b_summarization():
    """Phase 4b replaced everything between the system message and the last
    exchange with a summary -- including the implement prompt."""
    prompt = "# TICKET T1\n" + "REGION SOURCE\n" * 2000
    tight = Profile(
        name="test-tight-budget", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        round_input_token_budget=100,  # force 4b
        implementer_rules="t", reviewer_priorities="t",
    )
    out = compaction.compact_history(_history(prompt, 3), 4, tight)
    assert out[1]["content"] == prompt, "4b must not summarize away the ticket"
    assert any("PRIOR ROUNDS SUMMARY" in m["content"] for m in out)


def test_compaction_keeps_the_newest_exchange_intact():
    """The last assistant message is the candidate the next round revises;
    truncating it to 1000 chars removed the very text being edited."""
    h = _history("prompt", 2)
    out = compaction.compact_history(h, 3, PY)
    assert out[-1]["content"] == h[-1]["content"], "last feedback must be verbatim"
    assert out[-2]["content"] == h[-2]["content"], "last candidate must be verbatim"
    assert "COMPACTED" in out[2]["content"], "an older round should still be pruned"


def test_compaction_keeps_roles_alternating():
    """Emitting the summary as a second user turn produced [system, user, user],
    which the Anthropic Messages API rejects with a non-retryable 400."""
    tight = Profile(
        name="test-alt-budget", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        round_input_token_budget=100,
        implementer_rules="t", reviewer_priorities="t",
    )
    out = compaction.compact_history(_history("prompt", 3), 4, tight)
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    for a, b in zip(roles[1:], roles[2:]):
        assert a != b, f"consecutive {a} turns: {roles}"


def test_compaction_does_not_call_a_model_when_mechanical_fits():
    """The free path must be tried first; the LLM compactor was called on every
    over-budget round even when a mechanical summary would have fit."""
    tight = Profile(
        name="test-nollm-budget", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        round_input_token_budget=4000,
        implementer_rules="t", reviewer_priorities="t",
    )
    with patch("agent_loop.providers.chat") as spy:
        compaction.compact_history(_history("short prompt", 3), 4, tight)
    assert spy.call_count == 0


# ---------------------------------------------------------------------------
# memory -- learning feedback must record the finding, not the arbiter's prose
# ---------------------------------------------------------------------------
def test_learning_feedback_records_the_finding_text(tmp_path):
    memory.save_feedback(
        tmp_path, "T1", 1, "glm-5.2:cloud",
        "R1: the lock is held across the broker call", "BLOCKER", "REJECTED",
    )
    entries = memory.load_rejected_findings(tmp_path)
    assert entries[0]["finding"] == "R1: the lock is held across the broker call"
    assert entries[0]["severity"] == "BLOCKER"
    assert entries[0]["reviewer"] == "glm-5.2:cloud", "the author must not be '?'"
    ctx = memory.build_learning_context(tmp_path)
    assert "lock is held across the broker call" in ctx


def test_learning_context_deduplicates_across_rounds(tmp_path):
    for rnd in (1, 2, 3):
        memory.save_feedback(tmp_path, "T1", rnd, "m", "same finding", "MAJOR", "REJECTED")
    memory.save_feedback(tmp_path, "T1", 3, "m", "other finding", "MAJOR", "REJECTED")
    ctx = memory.build_learning_context(tmp_path)
    assert ctx.count("REJECTED: same finding") == 1, "one lesson must not fill the cap"
    assert "other finding" in ctx


def test_learning_context_empty_when_nothing_ruled(tmp_path):
    assert memory.build_learning_context(tmp_path) == ""


# ---------------------------------------------------------------------------
# arbiter -- the bar for UPHELD must come from the consumer, not from NT8
# ---------------------------------------------------------------------------
def test_arbiter_prompt_is_not_hardcoded_to_ninjatrader():
    """The prompt demanded that an upheld finding "loses money or leaves a
    position unprotected". No finding in a repo that does not trade can meet
    that, so the arbiter rejected everything and recommended SHIP."""
    generic = arbiter.arbiter_system()
    assert "NinjaTrader" not in generic
    assert "loses money" not in generic
    assert "UPHELD" in generic and "ESCALATE" in generic

    custom = arbiter.arbiter_system("Domain: a CSV parser. Blocking means silent data loss.")
    assert "silent data loss" in custom
    assert "UPHELD" in custom


def test_arbiter_prompt_carries_graph_context():
    prompt = arbiter.build_prompt(
        {"id": "T1", "title": "t", "defect": "d"}, [], "gates ok", "diff", (),
        context="Callers (2): a, b",
    )
    assert "Callers (2): a, b" in prompt


# ---------------------------------------------------------------------------
# models -- a role may hold several models; input tokens have their own rate
# ---------------------------------------------------------------------------
def test_registry_holds_a_multi_model_panel():
    """register() overwrote by role, so a two-family panel silently collapsed
    to whichever reviewer was registered last."""
    reg = models.ModelRegistry()
    reg.register(models.ModelConfig("glm-5.2:cloud", "reviewer", "fast", 0.0))
    reg.register(models.ModelConfig("minimax-m3:cloud", "reviewer", "fast", 0.0))
    assert [c.name for c in reg.get_all("reviewer")] == ["glm-5.2:cloud", "minimax-m3:cloud"]
    assert reg.get("reviewer").name == "glm-5.2:cloud"


def test_registry_rejects_an_arbiter_from_the_panels_family():
    reg = models.ModelRegistry()
    with pytest.raises(ValueError, match="same family"):
        reg.validate("kimi-k2.7-code", ["glm-5.2:cloud"], "ollama:glm-5.1:cloud")
    reg.validate("kimi-k2.7-code", ["glm-5.2:cloud"], "deepseek-v4-pro:cloud")


def test_cost_summary_prices_input_at_the_input_rate():
    reg = models.ModelRegistry()
    reg.register(models.ModelConfig(
        "m", "arbiter", "strong-reasoner", 25.0, cost_per_1m_in=5.0
    ))
    # 1M in at $5 + 1M out at $25
    assert reg.cost_summary("arbiter", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_num_ctx_fits_prompt_plus_completion():
    """num_ctx bounds prompt+completion, so asking for 48000 output tokens
    inside a fixed 32768 window was arithmetically impossible."""
    messages = [{"role": "user", "content": "x" * 40000}]  # ~10K tokens
    assert providers._fit_num_ctx(messages, 48000, 32768) >= 10000 + 48000
    assert providers._fit_num_ctx([{"role": "user", "content": "hi"}], 1000, 32768) == 32768


# ---------------------------------------------------------------------------
# providers -- prompt caching must be opt-in and must mark the stable head
# ---------------------------------------------------------------------------
def _anthropic_payload(messages, **kw):
    """Call _call_anthropic with the transport stubbed; return the sent payload."""
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured.update(payload)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with patch.object(providers, "_post", side_effect=fake_post):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}):
            providers._call_anthropic(
                "claude-opus-5", messages, 0.1, 1024, 900, 32768, None, kw.get("cache", False)
            )
    return captured


def _breakpoints(payload):
    """Count cache_control markers across system and messages."""
    n = 0
    system = payload.get("system")
    if isinstance(system, list):
        n += sum(1 for b in system if "cache_control" in b)
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            n += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
    return n


def test_single_shot_calls_place_no_cache_breakpoints():
    """A cache write bills at 1.25x input. The panel, the arbiter, plan/test/
    docs/brainstorm and the compactor each build a fresh prompt every time and
    can never read what they wrote, so marking their prompts was a pure 25%
    surcharge on every one of them."""
    payload = _anthropic_payload(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "review this"}]
    )
    assert _breakpoints(payload) == 0
    assert isinstance(payload["system"], str), "system must stay a plain string"
    assert payload["messages"][0]["content"] == "review this"


def test_multi_turn_marks_the_pinned_head_and_the_newest_turn():
    """Marking only the newest turn produced an entry that Phase 4a invalidated
    the moment it rewrote a prior round -- from then on every round paid a write
    premium and read nothing. turns[0] is the one span pin_count() guarantees is
    byte-identical across rounds."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "IMPLEMENT PROMPT with region source"},  # pinned
        {"role": "assistant", "content": "round 1 candidate"},
        {"role": "user", "content": "round 1 feedback"},                      # newest
    ]
    payload = _anthropic_payload(history, cache=True)

    marked = [
        i for i, m in enumerate(payload["messages"])
        if isinstance(m.get("content"), list)
        and any("cache_control" in b for b in m["content"])
    ]
    assert marked == [0, 2], f"expected the pinned head and the newest turn, got {marked}"
    assert payload["messages"][0]["content"][0]["text"].startswith("IMPLEMENT PROMPT")
    assert payload["messages"][2]["content"][0]["text"] == "round 1 feedback"
    # The assistant turn in between is left alone.
    assert payload["messages"][1]["content"] == "round 1 candidate"


def test_breakpoints_stay_within_the_anthropic_limit():
    """Anthropic allows at most four cache_control breakpoints per request."""
    history = [{"role": "system", "content": "sys"}]
    for i in range(6):
        history.append({"role": "assistant", "content": f"a{i}"})
        history.append({"role": "user", "content": f"u{i}"})
    payload = _anthropic_payload(history, cache=True)
    assert _breakpoints(payload) <= 4


def test_cache_is_a_noop_on_non_anthropic_backends():
    """ollama and openai have no cache_control API; the flag must not leak into
    their payloads or raise."""
    for backend in ("_call_ollama", "_call_openai"):
        fn = getattr(providers, backend)
        with patch.object(providers, "_post", return_value={
            "message": {"content": "ok"},
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }) as post:
            fn("m", [{"role": "user", "content": "hi"}], 0.1, 100, 900, 32768, None, True)
        sent = post.call_args[0][1]
        assert "cache_control" not in json.dumps(sent)


# ---------------------------------------------------------------------------
# regions -- named blocks in any language; line endings preserved
# ---------------------------------------------------------------------------
def test_extract_named_block_python():
    """The extractor required a C# modifier and return type, so on Python it
    matched nothing and reviewers were shown no acceptance tests at all."""
    src = (
        "import pytest\n\n"
        "def helper():\n    return 1\n\n"
        "def test_target():\n"
        "    # comment inside\n"
        "    assert helper() == 1\n\n"
        "def test_other():\n    pass\n"
    )
    got = regions.extract_named_block(src, "test_target", PY)
    assert got is not None
    assert "def test_target():" in got
    assert "assert helper() == 1" in got
    assert "def test_other" not in got, "the block must stop at the next declaration"


def test_extract_named_block_csharp_finds_the_declaration_not_the_call():
    src = (
        "public static void Main() {\n    Foo_Bar();\n}\n\n"
        "private static void Foo_Bar() {\n"
        "    Assert(true);\n"
        "}\n"
    )
    got = regions.extract_named_block(src, "Foo_Bar", CS)
    assert got is not None
    assert got.startswith("private static void Foo_Bar()")
    assert "Assert(true);" in got


def test_apply_preserves_crlf_and_missing_trailing_newline(tmp_path):
    """apply() rewrote every line in the platform terminator and always added a
    trailing newline, turning a two-line patch into a whole-file diff."""
    path = tmp_path / "x.cs"
    path.write_bytes(b"void A() {\r\n    int a = 1;\r\n}\r\nvoid B() {}")
    reg = regions.Region("R", "x.cs", path, "void A", "decl", 0, 2, "void A() {\n    int a = 1;\n}")
    regions.apply([reg], {"R": "void A() {\n    int a = 2;\n}"})
    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n\r" not in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "no bare LF may be introduced"
    assert raw.endswith(b"void B() {}"), "a missing trailing newline must stay missing"
    assert b"int a = 2;" in raw


def test_indent_region_spans_a_multi_line_signature(tmp_path):
    """A def whose parameters span several lines opens its block on the line
    where the brackets balance. Testing only the anchor line collapsed the
    region to that one line, `--list` reported OK, and splicing the
    replacement over it orphaned the old parameter list."""
    path = tmp_path / "m.py"
    path.write_text(
        "def check(\n"
        "    a: str,\n"
        "    b: int = 3,\n"
        ") -> bool:\n"
        "    if a:\n"
        "        return True\n"
        "    return False\n"
        "\n"
        "def after():\n"
        "    pass\n",
        encoding="utf-8",
    )
    regs = regions.extract(tmp_path, [{"id": "R", "file": "m.py", "anchor": "def check("}], PY)
    text = regs[0].text
    assert text.startswith("def check("), "the signature must be part of the region"
    assert "b: int = 3," in text, "the whole parameter list must be included"
    assert "return False" in text, "the body must be included"
    assert "def after" not in text, "the region must stop at the next declaration"


def test_single_line_signature_region_is_unchanged(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("def f(a):\n    return a\n\ndef g():\n    pass\n", encoding="utf-8")
    regs = regions.extract(tmp_path, [{"id": "R", "file": "m.py", "anchor": "def f("}], PY)
    assert regs[0].text == "def f(a):\n    return a"


def test_non_block_anchor_still_yields_one_line(tmp_path):
    """A bare statement is not a block opener, multi-line brackets or not."""
    path = tmp_path / "m.py"
    path.write_text("x = 1\ny = 2\n", encoding="utf-8")
    regs = regions.extract(tmp_path, [{"id": "R", "file": "m.py", "anchor": "x = 1"}], PY)
    assert regs[0].text == "x = 1"


def test_python_profile_docs_do_not_break_region_extraction(tmp_path):
    """The README and the Profile docstring both suggested block_comment=("#",)
    for Python, which made guard_unsupported_syntax refuse every Python file
    that contains a comment."""
    path = tmp_path / "m.py"
    path.write_text("# a comment\ndef f():\n    return 1\n", encoding="utf-8")
    regs = regions.extract(tmp_path, [{"id": "R", "file": "m.py", "anchor": "def f"}], PY)
    assert regs[0].text.startswith("def f():")


# ---------------------------------------------------------------------------
# workspace -- promotion must not overwrite uncommitted work
# ---------------------------------------------------------------------------
def test_promote_refuses_over_uncommitted_changes(tmp_path):
    """The worktree exists so the live tree is never clobbered; a plain copy on
    promote put the hazard straight back."""
    repo = _git_repo(tmp_path)
    (repo / "src" / "target.py").write_text("def f():\n    return 'MY WORK'\n", encoding="utf-8")

    with workspace.open_workspace(repo, "PROMOTE") as ws:
        (ws.root / "src" / "target.py").write_text("def f():\n    return 43\n", encoding="utf-8")
        with pytest.raises(workspace.WorkspaceError, match="uncommitted"):
            ws.promote(["src/target.py"])
        assert "MY WORK" in (repo / "src" / "target.py").read_text()
        # Explicit override still works for a caller who knows what they want.
        ws.promote(["src/target.py"], force=True)
        assert "return 43" in (repo / "src" / "target.py").read_text()


@pytest.mark.parametrize("newline,label", [(b"\n", "LF"), (b"\r\n", "CRLF")])
def test_exported_patch_applies_to_its_own_source(tmp_path, newline, label):
    """The exported patch is the human-review and promotion artifact. It was
    written through platform newline translation while `git diff` was read with
    universal newlines, so it came out CRLF regardless of the source: it applied
    to CRLF files by luck and `git apply` rejected it on every LF file."""
    repo = tmp_path / f"repo{label}"
    (repo / "src").mkdir(parents=True)
    body = b"def f():" + newline + b"    return 1" + newline
    (repo / "src" / "m.py").write_bytes(body)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )

    with workspace.open_workspace(repo, f"PATCH{label}") as ws:
        target = ws.root / "src" / "m.py"
        target.write_bytes(body.replace(b"return 1", b"return 2"))
        patch = ws.export_patch(tmp_path / f"out{label}.patch")
        assert patch is not None

    check = subprocess.run(
        ["git", "apply", "--check", str(patch)],
        cwd=repo, capture_output=True, text=True,
    )
    assert check.returncode == 0, (
        f"{label} patch must apply to its own source; git said: {check.stderr}"
    )


def test_promote_succeeds_on_a_clean_target(tmp_path):
    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "PROMOTE2") as ws:
        (ws.root / "src" / "target.py").write_text("def f():\n    return 43\n", encoding="utf-8")
        assert ws.promote(["src/target.py"]) == ["src/target.py"]
    assert "return 43" in (repo / "src" / "target.py").read_text()


# ---------------------------------------------------------------------------
# test mode -- must never touch the live working tree
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# context -- graph queries must use the symbol name, in any language
# ---------------------------------------------------------------------------
class _Reg:
    def __init__(self, anchor):
        self.anchor = anchor
        self.id = "R"
        self.file = "f"


@pytest.mark.parametrize(
    "anchor,expected",
    [
        ("def load_fused_data(", ["load_fused_data"]),
        ("async def fetch(", ["fetch"]),
        ("class Target:", ["Target"]),
        # C#: the whole anchor used to be sent to the graph as one "name".
        ("private void OnOrderUpdate(", ["OnOrderUpdate"]),
        ("public static bool TryCopy<T>(", ["TryCopy"]),
        ("func ServeHTTP(", ["ServeHTTP"]),
        ("re:^\\s*void Flatten", ["Flatten"]),
    ],
)
def test_graph_name_extraction_is_language_neutral(anchor, expected):
    from agent_loop.context import _extract_names_from_region
    assert _extract_names_from_region(_Reg(anchor)) == expected


def test_graph_freshness_marker_round_trip(tmp_path):
    """Nothing ever wrote the marker, so the check reported 'stale' forever and
    the status line carried no information."""
    from agent_loop import context

    prof = Profile(
        name="test-graph-fresh", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        graph_project="proj", implementer_rules="t", reviewer_priorities="t",
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    first = context.check_graph_freshness(tmp_path, prof)
    assert "never indexed" in first

    context.mark_graph_fresh(tmp_path)
    assert context.check_graph_freshness(tmp_path, prof) == "fresh"

    # An edit after the recorded index makes it stale again, with a duration.
    os.utime(tmp_path / "a.py", (time.time() + 7200, time.time() + 7200))
    assert context.check_graph_freshness(tmp_path, prof).startswith("stale (")


def test_graph_freshness_is_silent_without_a_project():
    prof = Profile(
        name="test-graph-none", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    )
    assert context.check_graph_freshness(Path("."), prof) == "no-project"


def test_test_mode_does_not_stash_the_users_work(tmp_path):
    """`--mode test` ran `git stash` on the live repo and then raised ValueError
    unpacking a string BEFORE `git stash pop` -- so the user's uncommitted work
    was left in a stash, and the command still exited 0."""
    from agent_loop.test_mode import run_test

    repo = _git_repo(tmp_path)
    (repo / "src" / "target.py").write_text("def f():\n    return 'WORK IN PROGRESS'\n", encoding="utf-8")

    tp = Profile(
        name="test-mode-regression", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        test_cmd="python -c \"print('==== 1 failed, 2 passed in 0.1s ====')\"",
        implementer_rules="t", reviewer_priorities="t",
    )
    register(tp)

    def mock_chat(model, messages, **kw):
        return Completion(
            text="<<<TESTS>>>\n```python\ndef test_new():\n    assert False\n```\n<<<END TESTS>>>",
            model=model,
        )

    with patch("agent_loop.test_mode.chat", side_effect=mock_chat):
        result = run_test(
            repo, "a defect", {"id": "T1", "expect_green": ["test_new"]},
            tp, "impl", test_file="tests/test_gen.py",
        )

    assert "WORK IN PROGRESS" in (repo / "src" / "target.py").read_text(), \
        "the live working tree must be untouched"
    stash = subprocess.run(["git", "stash", "list"], cwd=repo, capture_output=True, text=True)
    assert stash.stdout.strip() == "", f"no stash may be left behind: {stash.stdout}"
    assert result.get("error") is None, result.get("error")
    assert (repo / "tests" / "test_gen.py").exists()
