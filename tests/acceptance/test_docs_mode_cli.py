"""
Acceptance tests for the docs-mode CLI wiring.

`test_brainstorm_docs_modes.py` calls run_docs() directly, with the arguments
in the right order, so it passed throughout the period when docs mode could not
be invoked at all. The defect lived entirely in cli._docs():

  * run_docs was called POSITIONALLY against a signature it did not match, so
    `docs_type` received the implementer model name and every sub-mode of every
    invocation returned "unknown docs type: '<model>'";
  * there was no --docs-type argument, so three of the four sub-modes had no
    way to be selected even after the call was fixed;
  * --review-base was required for all four, though only changelog reads a diff;
  * --defect was never forwarded, so design/prd had no input; and
  * output_path was `args.test_file or "docs/UPDATES.md"` -- and --test-file
    defaults to tests/acceptance/test_generated.py, so the left side was never
    falsy and docs mode would have written markdown over a test file.

These tests therefore drive `main(argv)` so argparse is in the loop, and assert
on the kwargs run_docs actually receives.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli
from agent_loop.profiles import Profile, register

DOCS_PROFILE = Profile(
    name="test-docs-cli",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)
register(DOCS_PROFILE)

BASE_ARGV = ["--mode", "docs", "--profile", "test-docs-cli"]


def _run(argv):
    """Drive main() with run_docs stubbed; return (exit_code, captured_kwargs)."""
    captured = {}

    def fake_run_docs(repo, **kwargs):
        captured.update(kwargs)
        captured["repo"] = repo
        return {"docs": "# generated", "output_path": kwargs.get("output_path")}

    with patch("agent_loop.docs_mode.run_docs", fake_run_docs):
        code = cli.main(argv)
    return code, captured


@pytest.mark.parametrize("docs_type", ["changelog", "handover", "design", "prd"])
def test_every_sub_mode_forwards_its_own_docs_type(docs_type):
    """docs_type reaches run_docs, instead of the implementer model name."""
    argv = BASE_ARGV + ["--docs-type", docs_type]
    if docs_type == "changelog":
        argv += ["--review-base", "HEAD~1"]
    if docs_type in ("design", "prd"):
        argv += ["--defect", "add a trailing stop"]

    code, kwargs = _run(argv)
    assert code == 0, f"{docs_type} exited {code}"
    assert kwargs["docs_type"] == docs_type


def test_docs_type_is_never_the_model_name():
    """The exact defect: docs_type bound to the implementer."""
    code, kwargs = _run(BASE_ARGV + ["--review-base", "HEAD~1",
                                     "--implementer", "some-model:cloud"])
    assert code == 0
    assert kwargs["docs_type"] == "changelog"
    assert kwargs["implementer"] == "some-model:cloud"
    assert kwargs["docs_type"] != kwargs["implementer"]


def test_profile_receives_a_profile_not_a_ref_string():
    """`profile` used to receive args.review_base."""
    _, kwargs = _run(BASE_ARGV + ["--review-base", "HEAD~3"])
    assert isinstance(kwargs["profile"], Profile)
    assert kwargs["profile"].name == "test-docs-cli"
    assert kwargs["diff_ref"] == "HEAD~3"


def test_defect_is_forwarded_as_intent():
    """design/prd take their input from --defect."""
    _, kwargs = _run(BASE_ARGV + ["--docs-type", "design", "--defect", "add a trailing stop"])
    assert kwargs["intent"] == "add a trailing stop"


def test_output_path_is_not_the_acceptance_test_file():
    """Docs must never default to the --test-file path."""
    _, kwargs = _run(BASE_ARGV + ["--review-base", "HEAD~1"])
    out = kwargs["output_path"]
    assert "test_generated" not in out
    assert not out.startswith("tests/")
    assert out == "docs/generated/changelog.md"


@pytest.mark.parametrize("docs_type", ["changelog", "handover", "design", "prd"])
def test_default_output_path_is_per_sub_mode(docs_type):
    """Four sub-modes must not overwrite one another's output."""
    argv = BASE_ARGV + ["--docs-type", docs_type]
    if docs_type == "changelog":
        argv += ["--review-base", "HEAD~1"]
    if docs_type in ("design", "prd"):
        argv += ["--defect", "x"]
    _, kwargs = _run(argv)
    assert kwargs["output_path"] == f"docs/generated/{docs_type}.md"


def test_docs_out_overrides_the_default():
    _, kwargs = _run(BASE_ARGV + ["--docs-type", "handover", "--docs-out", "docs/my_handover.md"])
    assert kwargs["output_path"] == "docs/my_handover.md"


def test_non_changelog_sub_modes_do_not_require_review_base():
    """handover/design/prd read the codebase, not a diff."""
    code, kwargs = _run(BASE_ARGV + ["--docs-type", "handover"])
    assert code == 0, "handover must not demand --review-base"
    assert kwargs["docs_type"] == "handover"


def test_changelog_without_review_base_is_rejected():
    code, _ = _run(BASE_ARGV + ["--docs-type", "changelog"])
    assert code == 2


@pytest.mark.parametrize("docs_type", ["design", "prd"])
def test_design_and_prd_without_defect_are_rejected(docs_type):
    """Fail before spending a model call, not inside run_docs."""
    code, captured = _run(BASE_ARGV + ["--docs-type", docs_type])
    assert code == 2
    assert not captured, "should have refused before calling run_docs"


def test_invalid_docs_type_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        _run(BASE_ARGV + ["--docs-type", "nonsense"])
