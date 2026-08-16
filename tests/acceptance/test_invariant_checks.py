"""
Acceptance tests for T-INV1 and T-INV2: invariant enforcement.

T-INV1 (N1): the arbiter must not be the same model as any reviewer. The README
states this guarantee, but it was never enforced in code. A config setting
arbiter==reviewer silently collapses the separation of detection from
adjudication.

T-INV2 (N6): reviewers must be different models. The README states "different
model families review concurrently," but duplicate reviewers were accepted,
providing zero additional signal.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent_loop.loop import run_ticket
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-invariants",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)

TICKET = {
    "id": "T-INV",
    "title": "test ticket",
    "defect": "test defect",
    "spec": "test spec",
    "regions": [
        {"id": "R1", "file": "src/dummy.py", "anchor": "def dummy"}
    ],
}


def test_inv1_refuses_arbiter_same_as_reviewer(tmp_path):
    """run_ticket refuses when arbiter_model is in reviewers."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "dummy.py").write_text("def dummy():\n    pass\n", encoding="utf-8")
    result = run_ticket(
        repo=repo,
        ticket=TICKET,
        profile=PROFILE,
        implementer="model-a",
        reviewers=["model-b", "model-c"],
        arbiter_model="model-b",  # same as a reviewer!
    )
    assert result["final_verdict"] == "CONFIG_REJECTED"
    assert "arbiter" in result["detail"].lower()
    assert "model-b" in result["detail"]


def test_inv2_refuses_duplicate_reviewers(tmp_path):
    """run_ticket refuses when reviewers contains duplicates."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "dummy.py").write_text("def dummy():\n    pass\n", encoding="utf-8")
    result = run_ticket(
        repo=repo,
        ticket=TICKET,
        profile=PROFILE,
        implementer="model-a",
        reviewers=["model-b", "model-b"],  # duplicate!
        arbiter_model="model-c",
    )
    assert result["final_verdict"] == "CONFIG_REJECTED"
    assert "duplicate" in result["detail"].lower()
    assert "model-b" in result["detail"]


def test_inv_accepts_different_models(tmp_path):
    """run_ticket accepts when arbiter and reviewers are all different."""
    import subprocess
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "dummy.py").write_text("def dummy():\n    pass\n", encoding="utf-8")
    # Init a git repo so open_workspace can create a worktree.
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True,
                   env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
                        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test"})
    # This should not be refused on invariant grounds. It may fail later for
    # other reasons (no test command, etc.), but NOT with CONFIG_REJECTED.
    result = run_ticket(
        repo=repo,
        ticket=TICKET,
        profile=PROFILE,
        implementer="model-a",
        reviewers=["model-b", "model-c"],
        arbiter_model="model-d",
        max_rounds=1,
    )
    assert result["final_verdict"] != "CONFIG_REJECTED", (
        f"should not be refused: {result.get('detail', '')}"
    )