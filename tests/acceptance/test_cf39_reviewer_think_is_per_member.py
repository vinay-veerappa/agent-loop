"""
CF-39 -- `think` was per ROLE, and the panel mixes families that need opposite
settings.

glm-5.3-flash leaks its reasoning into the answer with thinking off, so the
reviewer role was set to `think=True` (CF-37). That setting reached every
member, and on its first real review (tvDownloadOHLC P1 T3, 2026-09-26)
deepseek-v4.1-flash spent the whole 64000-token budget on 227,501 chars of
reasoning and returned empty content -- the panel had one voter again, the
exact outcome CF-37 existed to end.

`RoleSettings.no_think_members` names members that run with thinking off. These
tests drive the real `review_panel` and the real registry with a config that
moves the value, so a literal cannot pass them by coincidence.
"""
from __future__ import annotations

import dataclasses

import pytest

from agent_loop import config
from agent_loop import loop as loop_mod
from agent_loop import models
from agent_loop.providers import Completion

REVIEW_BODY = (
    "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>"
)


def _activate(cfg):
    config.set_active(cfg)
    models.reload_default_registry()


@pytest.fixture
def restore_config():
    yield
    config.reset()
    models.reload_default_registry(config.DEFAULTS)


def _with_reviewer(**changes):
    base = config.DEFAULTS
    roles = dict(base.roles)
    roles["reviewer"] = dataclasses.replace(roles["reviewer"], **changes)
    return dataclasses.replace(base, roles=roles)


def _panel_thinks(tmp_path, monkeypatch, members, **kw):
    seen = {}

    def fake_chat(model, messages, **k):
        seen[model] = k.get("think")
        return Completion(text=REVIEW_BODY, model=model)

    monkeypatch.setattr(loop_mod, "chat", fake_chat)
    loop_mod.review_panel(list(members), "prompt", "system", tmp_path, 1, **kw)
    return seen


def test_a_no_think_member_reviews_with_thinking_off(restore_config, tmp_path, monkeypatch):
    _activate(_with_reviewer(
        model="a:cloud", extra_members=("b:cloud",), think=True,
        no_think_members=("b:cloud",),
    ))
    seen = _panel_thinks(tmp_path, monkeypatch, ["a:cloud", "b:cloud"])
    assert seen == {"a:cloud": True, "b:cloud": False}


def test_without_the_override_every_member_inherits_the_role(restore_config, tmp_path, monkeypatch):
    # negative control: the per-member result above is the override, not a default
    _activate(_with_reviewer(model="a:cloud", extra_members=("b:cloud",), think=True,
                             no_think_members=()))
    seen = _panel_thinks(tmp_path, monkeypatch, ["a:cloud", "b:cloud"])
    assert seen == {"a:cloud": True, "b:cloud": True}


def test_an_explicit_think_argument_still_wins_for_every_member(restore_config, tmp_path, monkeypatch):
    _activate(_with_reviewer(model="a:cloud", extra_members=("b:cloud",), think=True,
                             no_think_members=("b:cloud",)))
    seen = _panel_thinks(tmp_path, monkeypatch, ["a:cloud", "b:cloud"], think=True)
    assert seen == {"a:cloud": True, "b:cloud": True}


def test_the_registry_carries_the_per_member_setting(restore_config):
    _activate(_with_reviewer(model="a:cloud", extra_members=("b:cloud",), think=True,
                             no_think_members=("b:cloud",)))
    by_name = {c.name: c.think for c in models.DEFAULT_REGISTRY._configs["reviewer"]}
    assert by_name == {"a:cloud": True, "b:cloud": False}


def test_a_config_file_naming_a_non_member_is_refused():
    with pytest.raises(ValueError, match="not members"):
        config.merge(config.DEFAULTS, {"roles": {"reviewer": {"no_think_members": ["typo:cloud"]}}})


def test_a_json_override_is_accepted_as_a_list():
    cfg = config.merge(config.DEFAULTS, {"roles": {"reviewer": {
        "model": "a:cloud", "extra_members": ["b:cloud"], "no_think_members": ["b:cloud"],
    }}})
    rs = cfg.roles["reviewer"]
    assert rs.no_think_members == ("b:cloud",)
    assert rs.think_for("b:cloud") is False and rs.think_for("a:cloud") is rs.think


def test_the_shipped_panel_runs_deepseek_without_thinking():
    rs = config.DEFAULTS.roles["reviewer"]
    assert "deepseek-v4.1-flash:cloud" in rs.all_members
    assert rs.think_for("deepseek-v4.1-flash:cloud") is False
    assert rs.think_for(rs.model) is True, "glm-5.3-flash leaks reasoning with thinking off"


def test_an_inherited_override_follows_a_reseated_panel():
    # A consumer that swaps the second reviewer must not trip over a no-think
    # name it never wrote; the shipped deepseek entry simply falls away.
    cfg = config.merge(config.DEFAULTS, {"roles": {"reviewer": {"extra_members": ["x:cloud"]}}})
    assert cfg.roles["reviewer"].no_think_members == ()
    one = config.merge(config.DEFAULTS, {"roles": {"reviewer": {"extra_members": []}}})
    assert one.roles["reviewer"].all_members == (config.DEFAULTS.roles["reviewer"].model,)
