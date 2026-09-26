"""
CF-37: no role may name a model the catalogue records as RETIRED.

deepseek-v4-flash:0731-cloud was retired by Ollama on 2026-09-25 (HTTP 410) while
it was the default second reviewer, and every panel after that ran with one
voter. The loop degraded correctly (APPROVE_PARTIAL), but nothing refused the
config. The catalogue is where retirement is recorded, so the config is checked
against it -- every member, not only the primary, because it was an EXTRA member
that died.
"""
import pytest

from agent_loop import config


def _members():
    for role, rs in config.DEFAULTS.roles.items():
        for m in rs.all_members:
            yield role, m


@pytest.mark.parametrize("role,model", list(_members()))
def test_no_configured_member_is_retired(role, model):
    p = config.model_profile(model)
    assert p is not None, f"{role} member {model!r} is not catalogued"
    assert "RETIRED" not in p.note, f"{role} member {model!r} is retired: {p.note}"


@pytest.mark.parametrize("model", ["deepseek-v4-flash:0731-cloud", "qwen3.5:cloud"])
def test_the_retired_model_is_still_catalogued_as_retired(model):
    # Negative control: the check above passes vacuously if the marker is lost.
    # qwen3.5 (CF-40) was the default ARBITER when it died, and this test was
    # green throughout: retirement is only as visible as the note that records it.
    p = config.model_profile(model)
    assert p is not None and "RETIRED" in p.note


def test_the_reviewer_panel_is_two_families():
    fams = {m.split(":")[0].split("-")[0] for m in config.DEFAULTS.roles["reviewer"].all_members}
    assert len(fams) >= 2, fams
