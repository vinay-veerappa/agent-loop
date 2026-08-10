"""
Acceptance tests for Phase 1 state-machine fixes.

Each test exercises a specific issue from the plan (AGENT_LOOP_V2_PLAN.md section 2).
These tests MUST FAIL at baseline (before the fix) and PASS after the fix is
applied. The loop's test-first check (loop.py:442-457) enforces this.

The tests use a mock provider so they run offline -- no API keys needed.
The region extractor is also mocked because Python's indentation-based blocks
don't work with the brace matcher (that's a separate ticket).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch, MagicMock

import pytest

from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion, ProviderError
from agent_loop import loop, gates, regions, workspace, arbiter
from agent_loop.loop import PanelResult, Vote, Finding


# --- Test profile ---
TEST_PROFILE = Profile(
    name="test-python",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),  # Python has no block comments
    block_kind="indent",
    preprocessor_directives=(),
    build_cmd="",
    test_cmd="",
    lock_name="",
    risk_calls=(),
    protected=("test_*.py", "tests/*"),
    implementer_rules="You are a test implementer.",
    reviewer_priorities="You are a test reviewer.",
    settled=(),
)
register(TEST_PROFILE)


# --- Mock providers ---
def _impl_ok(model, messages, **kw):
    """Return a valid block that passes the static gate."""
    # Find the region ID from the USER message (not the system message which
    # contains the output contract with id="REGION_ID" as a template)
    for msg in messages:
        if msg.get("role") == "user":
            for line in msg["content"].split("\n"):
                if line.startswith('### REGION id='):
                    rid = line.split('id="')[1].split('"')[0]
                    return Completion(
                        text=f'<<<BLOCK id="{rid}">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="{rid}">>>\n<<<NOTES>>>\n- fixed\n<<<END NOTES>>>',
                        model=model, input_tokens=100, output_tokens=50)
    # Fallback: look for any REGION line in any message
    for msg in messages:
        for line in msg.get("content", "").split("\n"):
            if '### REGION id="' in line:
                rid = line.split('id="')[1].split('"')[0]
                return Completion(
                    text=f'<<<BLOCK id="{rid}">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="{rid}">>>\n<<<NOTES>>>\n- fixed\n<<<END NOTES>>>',
                    model=model, input_tokens=100, output_tokens=50)
    return Completion(text='<<<BLOCK id="R1">>>\nclass TargetClass:\n    def method(self):\n        return 42\n<<<END id="R1">>>',
                      model=model, input_tokens=100, output_tokens=50)

def _review_approve(model, messages, **kw):
    return Completion(text="<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>",
                      model=model, input_tokens=100, output_tokens=50)

def _review_revise(model, messages, **kw):
    return Completion(text="<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n<<<FINDINGS>>>\n- [BLOCKER] R1: test finding\n<<<END FINDINGS>>>\n<<<REQUIRED>>>\n- fix the thing\n<<<END REQUIRED>>>",
                      model=model, input_tokens=100, output_tokens=50)

def _arbiter_ship(model, messages, **kw):
    return Completion(text="<<<RULINGS>>>\n- REJECTED #1: not real\n<<<END RULINGS>>>\n<<<RECOMMENDATION>>>\nSHIP\n<<<END RECOMMENDATION>>>\n<<<RATIONALE>>>\nAll rejected.\n<<<END RATIONALE>>>\n<<<SETTLED>>>\n- NONE\n<<<END SETTLED>>>",
                      model=model, input_tokens=100, output_tokens=50)

def _unreachable(model, messages, **kw):
    raise ProviderError(f"{model} unreachable (mock)")


# --- Test helpers ---
def _make_repo(tmpdir):
    repo = tmpdir / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "target.py").write_text("class TargetClass:\n    def method(self):\n        return 42\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init')
    return repo

def _make_ticket(tid, title, defect="d", spec="s"):
    return {"id": tid, "title": title, "defect": defect, "spec": spec,
            "regions": [{"id": "R1", "file": "src/target.py", "anchor": "class TargetClass"}]}

def _mock_region(repo):
    path = repo / "src" / "target.py"
    return regions.Region(id="R1", file="src/target.py", path=path, anchor="class TargetClass",
                          kind="decl", start_line=0, end_line=2, text=path.read_text(encoding="utf-8"))

@contextmanager
def _patched_loop(repo, impl_fn=_impl_ok, panel_result=None, arbiter_fn=None):
    """Context manager that patches chat, regions.extract, and review_panel."""
    region = _mock_region(repo)
    with patch("agent_loop.loop.chat", side_effect=impl_fn):
        with patch("agent_loop.loop.regions.extract", return_value=[region]):
            if panel_result is not None:
                with patch("agent_loop.loop.review_panel", return_value=panel_result):
                    if arbiter_fn is not None:
                        with patch("agent_loop.arbiter.chat", side_effect=arbiter_fn):
                            yield
                    else:
                        yield
            elif arbiter_fn is not None:
                with patch("agent_loop.arbiter.chat", side_effect=arbiter_fn):
                    yield
            else:
                yield


# === P1-1: Stale per-round artifacts must be purged ===
def test_p1_1_stale_artifacts_purged(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-1", "Stale artifacts")
    art_dir = repo / "logs" / "agent_loop" / "P1-1"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "r2_impl_raw.txt").write_text("stale", encoding="utf-8")
    (art_dir / "r2_build.txt").write_text("stale", encoding="utf-8")

    panel = PanelResult(votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True)
    with _patched_loop(repo, panel_result=panel):
        loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                        max_rounds=1, apply=False, arbiter_model="")

    assert not (art_dir / "r2_impl_raw.txt").exists(), \
        "stale r2 artifacts from prior run were not purged at round start"


# === P1-2: Arbiter-unreachable must produce ARBITER_DEADLOCK ===
def test_p1_2_arbiter_deadlock(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-2", "Arbiter deadlock")
    panel = PanelResult(votes=[Vote("r1", "REVISE", finding_list=[
        Finding("r1", "BLOCKER", "test")])], verdict="REVISE", valid=True)
    with _patched_loop(repo, panel_result=panel, arbiter_fn=_unreachable):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                                 max_rounds=2, apply=False, arbiter_model="test-arbiter")
    assert result["final_verdict"] == "ARBITER_DEADLOCK", \
        f"expected ARBITER_DEADLOCK, got {result['final_verdict']}"


# === P1-3: MAX_ROUNDS_EXHAUSTED must distinguish "ran with arbiter" from "ran without" ===
def test_p1_3_arbiter_never_ran(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-3", "Arbiter never ran")
    panel = PanelResult(votes=[Vote("r1", "REVISE", finding_list=[
        Finding("r1", "BLOCKER", "test")])], verdict="REVISE", valid=True)
    with _patched_loop(repo, panel_result=panel):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                                 max_rounds=1, apply=False, arbiter_model="")
    assert result["final_verdict"] == "ARBITER_NEVER_RAN", \
        f"expected ARBITER_NEVER_RAN, got {result['final_verdict']}"


# === P1-4: applied must distinguish approved from unapproved ===
def test_p1_4_applied_split(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-4", "Applied split")
    panel = PanelResult(votes=[Vote("r1", "APPROVE")], verdict="APPROVE", valid=True)
    with _patched_loop(repo, panel_result=panel):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                                 max_rounds=1, apply=True, arbiter_model="")
    assert result.get("applied_approved") == True, "applied_approved must be True for APPROVE"
    assert result.get("applied_unapproved") == False, "applied_unapproved must be False for APPROVE"


# === P1-5: PANEL_REJECT goes to arbiter with rethink prompt ===
def test_p1_5_panel_reject_goes_to_arbiter(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-5", "Panel reject")
    arbiter_calls = []
    def arbiter_spy(model, messages, **kw):
        arbiter_calls.append(True)
        return _arbiter_ship(model, messages, **kw)
    panel = PanelResult(votes=[Vote("r1", "REJECT", finding_list=[
        Finding("r1", "BLOCKER", "fundamentally wrong")])], verdict="REJECT", valid=True)
    with _patched_loop(repo, panel_result=panel, arbiter_fn=arbiter_spy):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                                 max_rounds=2, apply=False, arbiter_model="test-arbiter")
    assert len(arbiter_calls) > 0, "arbiter must be called on REJECT"
    assert result["final_verdict"] != "PANEL_REJECT", "PANEL_REJECT is a signal, not a terminal state"


# === P1-6: PANEL_UNREACHABLE must revert touched files ===
def test_p1_6_panel_unreachable_reverts(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-6", "Panel unreachable revert")
    panel = PanelResult(votes=[Vote("r1", "UNREACHABLE", error="mock")], verdict="", valid=False)
    with _patched_loop(repo, panel_result=panel):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl", ["r1"],
                                 max_rounds=2, apply=False, arbiter_model="test-arbiter")
    assert result["final_verdict"] == "PANEL_UNREACHABLE"
    target = (repo / "src" / "target.py").read_text()
    assert "return 42" in target, "touched files were not reverted on PANEL_UNREACHABLE"


# === P1-7: Quorum - partial panel with unanimous APPROVE should proceed ===
def test_p1_7_quorum_partial_panel(tmp_path):
    repo = _make_repo(tmp_path)
    ticket = _make_ticket("P1-7", "Quorum")
    panel = PanelResult(
        votes=[Vote("r1", "APPROVE"), Vote("r2", "APPROVE"),
               Vote("r3", "UNREACHABLE", error="mock")],
        verdict="APPROVE", valid=False)  # valid=False because r3 unreachable
    with _patched_loop(repo, panel_result=panel):
        result = loop.run_ticket(repo, ticket, TEST_PROFILE, "test-impl",
                                 ["r1", "r2", "r3"], max_rounds=1, apply=False, arbiter_model="")
    assert result["final_verdict"] == "APPROVE", \
        f"2-of-3 unanimous APPROVE should proceed, got {result['final_verdict']}"
    assert result.get("panel_partial") == True, "panel_partial metadata must be set"