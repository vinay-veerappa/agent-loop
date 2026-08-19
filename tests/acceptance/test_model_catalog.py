"""
The model catalogue must describe the models we actually use, and must not
contradict the config that uses them.

A catalogue nobody checks is a comment that rots. These guards are cheap and
they fail loudly the moment config and catalogue disagree.
"""
import os

import pytest

from agent_loop import config


def _roles():
    return config.DEFAULTS.roles.items()


@pytest.mark.parametrize("role,rs", list(_roles()))
def test_every_configured_model_is_catalogued(role, rs):
    p = config.model_profile(rs.model)
    assert p is not None, (
        f"{role} uses {rs.model!r}, which has no MODEL_CATALOG entry. Harvest it "
        f"with `ollama show {rs.model}` and record what it is."
    )


@pytest.mark.parametrize("role,rs", list(_roles()))
def test_a_role_is_only_assigned_a_model_the_catalogue_says_suits_it(role, rs):
    p = config.model_profile(rs.model)
    assert role in p.suited, (
        f"{role} uses {rs.model!r}, whose catalogue entry does not list {role!r} "
        f"as suited (lists {p.suited}). Either the assignment is wrong or the "
        f"catalogue is stale -- deepseek-v4-pro looked like the obvious arbiter "
        f"and measured 0/5, so resolve this rather than widening `suited`."
    )


@pytest.mark.parametrize("role,rs", list(_roles()))
def test_thinking_is_not_enabled_on_a_model_that_cannot_think(role, rs):
    """think=True on a model with no reasoning mode is not a trade-off, it is a
    no-op that reads like a decision. mistral-large-3, the current arbiter, has
    no thinking capability at all."""
    p = config.model_profile(rs.model)
    if rs.think:
        assert p.thinking, (
            f"{role} sets think=True but {rs.model!r} has no thinking capability "
            f"per `ollama show`. That setting does nothing."
        )


@pytest.mark.parametrize("role,rs", list(_roles()))
def test_the_output_budget_fits_the_context(role, rs):
    """num_predict is bounded by the context window; asking for more output than
    the model can hold is arithmetically impossible and the server truncates."""
    p = config.model_profile(rs.model)
    assert rs.max_tokens < p.context_tokens, (
        f"{role}: max_tokens={rs.max_tokens} against a {p.context_tokens}-token "
        f"context for {rs.model!r}"
    )


def test_catalogue_costs_are_stated_not_implied():
    """0.0 means 'not metered per token' for a subscription model, and the
    Anthropic entries must carry real prices so a switch to them is visibly a
    cost change rather than a silent one."""
    for name, p in config.MODEL_CATALOG.items():
        # Keyed by how the model is REACHED, not by family. `claude-sonnet-5`
        # over the Anthropic API is metered per token; `agy:claude-sonnet-4-6`
        # is the same family through the Antigravity subscription and is not.
        #
        # O62: this used to test `name.startswith("claude-")`, which was the
        # right INTENT read off the wrong thing -- the bare name. The bare names
        # were the defect: they dispatched to ollama and 404'd. Now that the key
        # carries its backend, the predicate can be what the comment above
        # already said it was.
        if name.startswith("anthropic:"):
            assert p.cost_per_1m_in > 0 and p.cost_per_1m_out > 0, name
        else:
            assert p.cost_per_1m_in == 0.0 and p.cost_per_1m_out == 0.0, name


def test_measured_claims_say_so():
    """`suited` is a claim. Anything asserting suitability for a role should
    carry a note explaining why, so a future reader can tell a measurement from
    a guess."""
    for name, p in config.MODEL_CATALOG.items():
        if p.suited:
            assert p.note.strip(), f"{name} claims roles {p.suited} with no note"


# ---------------------------------------------------------------------------
# Per-mode model selection
# ---------------------------------------------------------------------------
def test_a_mode_can_name_its_own_model():
    """Every non-patch mode ran on the implementer -- a code-specialised model --
    including docs, which writes prose. ModeSettings had no `model` field, so
    there was no way to express a different choice."""
    assert hasattr(config.ModeSettings("x", 1, True), "model") or True
    m = config.ModeSettings(max_tokens=1000, think=False, model="some-model")
    assert m.model == "some-model"


def test_modes_inherit_the_implementer_by_default():
    """The mechanism exists; no assignment has been changed without evidence."""
    for name, m in config.DEFAULTS.modes.items():
        assert m.model == "", (
            f"mode {name!r} pins {m.model!r}. That is a claim -- record the "
            f"measurement that justifies it in MODEL_CATALOG before pinning."
        )


def test_a_mode_model_must_be_catalogued_and_suited():
    """If a mode does pin a model, the same standard applies as for roles."""
    for name, m in config.DEFAULTS.modes.items():
        if not m.model:
            continue
        p = config.model_profile(m.model)
        assert p is not None, f"mode {name} pins uncatalogued {m.model!r}"
        assert m.max_tokens < p.context_tokens
        if m.think:
            assert p.thinking, f"mode {name} sets think=True on a model that cannot"


def test_a_pinned_mode_model_actually_reaches_the_mode(tmp_path):
    """Drives main(argv) through the REAL mechanism -- a config file and
    --config -- so argparse, config.load and the resolution path are all in the
    loop. O10 and O15 were both defects in CLI wiring that library-level tests
    could not see.

    Note main() reloads config from disk unconditionally, so a test that calls
    config.set_active() beforehand proves nothing: it is discarded. This test
    was written that way first and passed a broken assumption.
    """
    import json
    from unittest.mock import patch as mpatch
    from agent_loop import cli, models as _models

    seen = {}

    def fake_run_docs(*a, **kw):
        seen["model"] = kw.get("implementer") or (a[3] if len(a) > 3 else None)
        return {"doc": "", "path": None}

    cfg = tmp_path / "agent_loop.config.json"
    cfg.write_text(json.dumps({"modes": {"docs": {"model": "glm-5.2:cloud"}}}), encoding="utf-8")
    old_cwd = os.getcwd()
    try:
        with mpatch("agent_loop.docs_mode.run_docs", side_effect=fake_run_docs):
            cli.main([
                "--mode", "docs", "--docs-type", "handover", "--config", str(cfg),
                "--profile", "agent-loop-self", "--profile-module", "profiles.self",
            ])
    finally:
        os.chdir(old_cwd)
        config.reset()
        _models.reload_default_registry(config.DEFAULTS)

    assert seen.get("model") == "glm-5.2:cloud", seen
