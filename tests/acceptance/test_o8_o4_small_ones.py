"""
O8 and O4: the small unticketed ones, and the report's coupled correlation.

Grouped because they were filed together and each is a few lines. Two of them are
not cosmetic:

* `check_lint`'s digest matched only the MSBuild `error CS1234` shape, so on a
  ruff or eslint run nothing matched and the model was handed a raw 4000-char
  tail instead of the errors. A gate whose feedback is unreadable is a gate the
  model cannot act on.
* `replay` adjudicated with different arbiter RULES and un-injected settled
  decisions than the real pipeline. Replay exists to hold everything but one
  variable constant, so a divergence there does not merely add noise -- it makes
  every flip it reports meaningless, which is what O2 was about.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import cli, gates, report
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-small",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)


# --------------------------------------------------------------------------
# O8: the lint digest understood only MSBuild
# --------------------------------------------------------------------------
RUFF = """\
src/agent_loop/loop.py:41:1: F401 [*] `math` imported but unused
src/agent_loop/cli.py:12:80: E501 Line too long (95 > 88)
Found 2 errors.
"""

ESLINT = """\
/repo/src/app.ts
  12:1  error  'foo' is defined but never used  no-unused-vars
  18:7  warning  Unexpected console statement   no-console
"""

MSBUILD = """\
RiskGuard.cs(120,13): error CS1002: ; expected
RiskGuard.cs(121,5): warning CS0168: variable declared but never used
"""


def test_a_ruff_style_lint_line_is_digested():
    d = gates.lint_digest(RUFF)
    assert "F401" in d
    assert "E501" in d
    assert "Found 2 errors" not in d, "the summary line is not a diagnostic"


def test_an_eslint_style_line_is_digested():
    d = gates.lint_digest(ESLINT)
    assert "no-unused-vars" in d


def test_the_msbuild_shape_still_works():
    """The C# profile depends on it, so widening must not narrow."""
    d = gates.lint_digest(MSBUILD)
    assert "CS1002" in d


def test_the_digest_is_not_the_whole_raw_tail():
    """The defect: nothing matched, so the model got output[-4000:] and had to
    find the errors itself."""
    noisy = ("irrelevant chatter\n" * 50) + RUFF
    d = gates.lint_digest(noisy)
    assert "irrelevant chatter" not in d, d
    assert "F401" in d


def test_check_lint_feedback_carries_the_digest(tmp_path):
    from _interp import PY_EXE

    # The noise matters. With a short output the `output[-4000:]` FALLBACK also
    # contains the diagnostic, so asserting only "F401 in feedback" passes whether
    # the digest ran or not -- and reverting check_lint to the MSBuild-only digest
    # left this test green. The chatter has to be long enough that the tail would
    # be visible if the digest had not filtered it.
    cmd = PY_EXE + " -c \"" + (
        "import sys;"
        "print('irrelevant chatter ' * 400);"
        "print('src/a.py:1:1: F401 [*] `os` imported but unused');"
        "print('irrelevant chatter ' * 400);"
        "sys.exit(1)"
    ) + "\""
    res = gates.check_lint(cmd, tmp_path)
    assert not res.ok
    assert "F401" in res.feedback, res.feedback
    assert "irrelevant chatter" not in res.feedback, (
        "the raw tail was handed to the model instead of the diagnostics"
    )


# --------------------------------------------------------------------------
# O8: report needs neither a profile nor a panel
# --------------------------------------------------------------------------
def test_report_mode_does_not_require_a_profile(tmp_path, capsys):
    with patch("agent_loop.report.run_report", return_value=0) as rr:
        code = cli.main(["--mode", "report"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "--profile is required" not in out
    assert rr.called, "report mode did not reach run_report"


def test_report_mode_does_not_warn_about_the_panel(capsys):
    """It reads a ledger. There is no panel to be one-membered."""
    with patch("agent_loop.report.run_report", return_value=0) as rr:
        cli.main(["--mode", "report"])
    assert "panel has one member" not in capsys.readouterr().out


def test_a_mode_that_does_need_a_profile_still_says_so(capsys):
    code = cli.main(["--mode", "plan", "--defect", "d"])
    assert code == 2
    assert "--profile is required" in capsys.readouterr().out


# --------------------------------------------------------------------------
# O8: replay must adjudicate exactly as the loop does
# --------------------------------------------------------------------------
def test_replay_passes_the_profiles_arbiter_rules():
    """Replay's whole purpose is to hold everything constant but one variable.
    Adjudicating under the DEFAULT rules while the loop used the profile's is a
    difference that makes any flip it reports meaningless."""
    import inspect

    from agent_loop import replay

    src = inspect.getsource(replay)
    # Asserted on the whole module, not a slice: the first attempt cut the call at
    # the first ")", which lands inside the synthetic-ticket dict, so it read a
    # fragment and reported a fix that was there as missing.
    assert "rules=profile.arbiter_rules" in src, (
        "replay adjudicates under the DEFAULT arbiter rules while the loop used "
        "the profile's"
    )


def test_replay_injects_settled_decisions_like_the_loop_does():
    import inspect

    from agent_loop import replay

    src = inspect.getsource(replay)
    assert "inject_settled" in src, (
        "replay reads profile.settled directly; the loop injects auto-extracted "
        "decisions first, so the two see different prompts"
    )


def test_replay_documents_only_flags_that_exist():
    """O8 said the docstring advertised `--replay-dir`, which the CLI never
    implemented. **That entry is stale** -- the flag exists (cli.py, `--replay-dir`,
    nargs="*"), so the docstring is correct and the assertion is the other way
    round: every flag the docstring names must be a real argument.

    Written this way rather than deleted because the invariant is the useful part,
    and because the first version of this test asserted the flag's ABSENCE and
    would have had me "fix" correct code."""
    import re as _re

    from agent_loop import cli, replay

    doc = replay.__doc__ or ""
    parser_src = __import__("inspect").getsource(cli)
    for flag in set(_re.findall(r"--[a-z][a-z0-9-]+", doc)):
        assert f'"{flag}"' in parser_src, f"{flag} is documented but not implemented"


def test_replay_has_no_unused_imports():
    import inspect

    from agent_loop import replay

    src = inspect.getsource(replay)
    body = src.split('"""', 2)[-1]
    for name in ("Completion", "ProviderError", "PanelResult", "RoundRecord"):
        assert f" {name}" not in body.replace(f"import", ""), (
            f"{name} is imported and never used"
        )


# --------------------------------------------------------------------------
# O4: the correlation compared coupled variables
# --------------------------------------------------------------------------
def test_upheld_is_normalised_per_round_not_summed_across_them():
    """`upheld_per_ticket` summed upheld findings ACROSS rounds while the
    y-variable IS the round count, so more rounds mechanically meant more
    recorded findings. The metric reported "the arbiter is upholding noise"
    almost regardless of arbiter quality."""
    import inspect

    src = inspect.getsource(report)
    assert "upheld_per_round" in src, (
        "the x-variable is still a sum across rounds, which is mechanically "
        "coupled to the y-variable"
    )


def test_the_report_says_what_the_correlation_means(tmp_path):
    """A number with a known confound must carry the caveat, or it will be read
    as a measurement of the arbiter."""
    import inspect

    src = inspect.getsource(report)
    assert "coupled" in src.lower() or "confound" in src.lower()
