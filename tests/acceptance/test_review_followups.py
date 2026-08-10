"""
Acceptance tests for the 2026-08-10 follow-up review (commit f2103cd).

Test-first: every test here is RED at the commit that introduces it, and is
the acceptance criterion for one ticket in tickets/review_followups.json.

IMPORT DISCIPLINE: names that do not exist yet are imported INSIDE the test
body, never at module level. A module-level ImportError is a pytest collection
*error*, and an errored suite cannot establish an expected-failure baseline
(workspace.capture_baseline refuses it) — which would make every ticket on
this profile unrunnable rather than merely red.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.loop import Finding
from agent_loop.profiles import Profile, register


# ---------------------------------------------------------------------------
# T1 — Finding.signature must survive incidental rewording
# ---------------------------------------------------------------------------
def test_signature_ignores_incidental_variation():
    """The thrashing detector treats two rounds as non-overlapping when no
    signature matches. A signature that changes because a reviewer quoted a
    different line number makes `thrashing()` fire on a converging ticket.

    The first-200-characters signature retains digits and punctuation, so it is
    strictly MORE fragile here than the alpha-only word list it replaced.
    """
    a = Finding("m", "BLOCKER", "R1: the lock is held during a broker call at line 412")
    b = Finding("m", "BLOCKER", "R1: the lock is held during a broker call at line 415")
    assert a.signature == b.signature

    # Punctuation and casing are incidental too.
    c = Finding("m", "BLOCKER", "R1 -- the lock is held during a broker call, at line 9")
    assert a.signature == c.signature


def test_signature_still_distinguishes_distinct_findings():
    """Guard against the degenerate fix: a signature that collapses everything
    to one value would pass the test above and disable thrashing detection
    entirely. Two genuinely different findings must not collide.
    """
    a = Finding("m", "BLOCKER", "the lock is held during a broker call")
    b = Finding("m", "BLOCKER", "the timer is armed twice and never disposed")
    assert a.signature != b.signature
    assert a.signature.strip() != ""


# ---------------------------------------------------------------------------
# T2 — the lint gate must look at the patch, not the whole tree
# ---------------------------------------------------------------------------
def test_check_lint_substitutes_touched_files(tmp_path):
    """A lint_cmd without {files} lints the entire worktree, so one
    pre-existing lint error anywhere blocks every round of every ticket
    forever. check_compile already takes `files`; check_lint must too.
    """
    from agent_loop.gates import check_lint

    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")

    ok = check_lint("python -m py_compile {files}", tmp_path, files=["clean.py"])
    assert ok.ok, ok.detail

    bad = check_lint("python -m py_compile {files}", tmp_path, files=["broken.py"])
    assert not bad.ok, "the gate must fail on the file the patch touched"


def test_check_lint_is_a_noop_when_nothing_was_touched(tmp_path):
    """With a {files} placeholder and no touched files there is nothing to
    lint; the gate must pass rather than lint the tree or emit an empty
    command."""
    from agent_loop.gates import check_lint

    res = check_lint("python -m py_compile {files}", tmp_path, files=[])
    assert res.ok
    assert "no files" in res.summary.lower()


# ---------------------------------------------------------------------------
# T3 — cache writes must be captured and priced
# ---------------------------------------------------------------------------
def test_cost_includes_the_cache_write_premium():
    """Anthropic bills cache writes at 1.25x input. Those tokens are reported
    in `cache_creation_input_tokens`, NOT in `input_tokens`, so a cost model
    that ignores them reports cache writes as free — and the loop cannot tell
    whether enabling caching made a ticket cheaper or dearer.
    """
    from agent_loop.providers import Completion

    c = Completion(
        text="", model="claude-opus-5",
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    # claude-opus-5 input is $5.00/1M; a write bills at 1.25x.
    assert c.cost_usd == pytest.approx(6.25)


def test_cache_creation_tokens_are_captured_from_usage():
    """The field has to be populated from the API response, not merely exist."""
    from agent_loop import providers

    payload = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 1234,
            "cache_read_input_tokens": 99,
        },
    }
    with patch.object(providers, "_post", return_value=payload):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            out = providers._call_anthropic(
                "claude-opus-5", [{"role": "user", "content": "hi"}],
                0.1, 1024, 900, 32768,
            )
    assert out.cache_creation_tokens == 1234
    assert out.cache_read_tokens == 99


# ---------------------------------------------------------------------------
# T4 — reviewer overlap cannot be detected by string equality
# ---------------------------------------------------------------------------
def test_same_finding_matches_across_rewording():
    """The per-reviewer marginal-value metric asks "did the other reviewer
    raise this finding too?" using exact string equality. Two models never
    phrase a finding identically, so the answer is always no, every upheld
    finding scores as unique, and no redundant reviewer is ever identified —
    which is the only thing the metric exists to do.
    """
    from agent_loop.report import _same_finding

    a = "R1: the lock is held across the broker call at line 412"
    b = "R1 -- lock is held across the broker call (line 415)"
    assert _same_finding(a, b)

    c = "the retry timer is never disposed"
    assert not _same_finding(a, c)


# ---------------------------------------------------------------------------
# T5 — the correlation is arithmetically biased toward zero
# ---------------------------------------------------------------------------
def test_pearson_is_exact_on_perfectly_correlated_data():
    """The report mixes a POPULATION covariance (divides by n) with a SAMPLE
    standard deviation (statistics.stdev divides by n-1), which scales |r| by
    (n-1)/n — 33% low at n=3. Perfectly correlated data must give exactly 1.0.
    """
    from agent_loop.report import _pearson

    assert _pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert _pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_handles_zero_variance_without_dividing_by_zero():
    from agent_loop.report import _pearson

    assert _pearson([2, 2, 2], [1, 2, 3]) == 0.0
    assert _pearson([], []) == 0.0


# ---------------------------------------------------------------------------
# T6 — replay mode must not traceback on a repo that has never run
# ---------------------------------------------------------------------------
def test_replay_mode_without_logs_dir_exits_cleanly(tmp_path, monkeypatch):
    """`log_root.iterdir()` runs before the "no recorded tickets" check, so on
    a fresh clone --mode replay raises FileNotFoundError instead of reporting
    that there is nothing to replay."""
    from agent_loop.cli import main

    prof = Profile(
        name="test-replay-missing-logs",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    )
    register(prof)

    monkeypatch.chdir(tmp_path)
    code = main(["--mode", "replay", "--profile", "test-replay-missing-logs"])
    assert code == 1
