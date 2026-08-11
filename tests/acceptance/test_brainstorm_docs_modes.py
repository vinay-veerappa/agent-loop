"""
Acceptance tests for brainstorm and docs modes.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion
from agent_loop.brainstorm_mode import run_brainstorm
from agent_loop.docs_mode import run_docs


PROFILE = Profile(
    name="test-brainstorm",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)
register(PROFILE)


def test_brainstorm_parses_approaches():
    """run_brainstorm returns approaches and recommendation."""
    def mock_chat(model, messages, **kw):
        return Completion(
            text=(
                "<<<APPROACHES>>>\n"
                "## Approach 1: Fix the loop\n"
                "Add a guard.\n\n"
                "**Pros**: Simple\n**Cons**: Limited\n**Effort**: small\n\n"
                "## Approach 2: Rewrite the module\n"
                "Start fresh.\n\n"
                "**Pros**: Clean\n**Cons**: Risky\n**Effort**: large\n"
                "<<<END APPROACHES>>>\n"
                "<<<RECOMMENDATION>>>\nApproach 1 is safer.\n<<<END RECOMMENDATION>>>"
            ),
            model=model, input_tokens=100, output_tokens=200,
        )

    import tempfile
    tmpdir = tempfile.mkdtemp()
    repo = Path(tmpdir)
    (repo / "logs" / "agent_loop" / "BRAINSTORM").mkdir(parents=True, exist_ok=True)

    with patch("agent_loop.brainstorm_mode.chat", side_effect=mock_chat):
        result = run_brainstorm(repo, "test defect", PROFILE, "test-impl")

    assert result["approaches"] is not None
    assert "Approach 1" in result["approaches"]
    assert "Approach 2" in result["approaches"]
    assert "Approach 1 is safer" in result["recommendation"]


def test_brainstorm_handles_unreachable():
    """run_brainstorm handles unreachable model gracefully."""
    from agent_loop.providers import ProviderError

    def mock_chat(model, messages, **kw):
        raise ProviderError("unreachable")

    import tempfile
    tmpdir = tempfile.mkdtemp()
    repo = Path(tmpdir)
    (repo / "logs" / "agent_loop" / "BRAINSTORM").mkdir(parents=True, exist_ok=True)

    with patch("agent_loop.brainstorm_mode.chat", side_effect=mock_chat):
        result = run_brainstorm(repo, "test defect", PROFILE, "test-impl")

    assert "error" in result
    assert result["approaches"] is None


def test_docs_parses_output(tmp_path):
    """run_docs returns documentation content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init --allow-empty')

    # Create a commit with a change
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    os.system(f'cd /d "{repo}" && git add -A && git commit -m "change"')

    def mock_chat(model, messages, **kw):
        return Completion(
            text=(
                "<<<DOCS>>>\n# Updates\n\nChanged x to 1.\n<<<END DOCS>>>\n"
                "<<<NOTES>>>\n- Updated docs for x.py change\n<<<END NOTES>>>"
            ),
            model=model, input_tokens=100, output_tokens=200,
        )

    with patch("agent_loop.docs_mode.chat", side_effect=mock_chat):
        result = run_docs(repo, PROFILE, "test-impl",
                         docs_type="changelog", diff_ref="HEAD~1",
                         output_path="docs/UPDATES.md")

    assert result["docs"] is not None
    assert "Changed x to 1" in result["docs"]
    assert (repo / "docs" / "UPDATES.md").exists()


def test_docs_handles_no_diff(tmp_path):
    """run_docs handles empty diff gracefully."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f'cd /d "{repo}" && git init && git add -A && git commit -m init --allow-empty')

    result = run_docs(repo, PROFILE, "test-impl",
                     docs_type="changelog", diff_ref="HEAD")
    assert "error" in result
    assert "docs" not in result or result.get("docs") is None