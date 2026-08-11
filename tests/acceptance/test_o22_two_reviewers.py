"""
O22: the panel must have at least two members, from different viewpoints.

The requirement, stated by the user:

  "we should always have at least two doing the review preferably from different
   view points"

That is a policy, not just a schema gap, so it is encoded three ways rather than
one: the schema can EXPRESS several members, the shipped default IS two from
different families, and a static guard fails the build if either stops being true.
A default that quietly drops to one member is exactly how this survived — every
command in HANDOVER §5 passes `--reviewers` explicitly, so nobody ran the default.

The schema call, decided as option A: `RoleSettings.extra_members` names members
BEYOND `model`, so `model` stays the single primary truth and overriding it still
works. `ModelRegistry` already stores `role -> [config]`
and appends, so the registry could always hold a panel; `registry_from_config`
could only ever put one member in it. This makes the capability reachable without
changing what `model` means to its several readers.
"""
import pytest

from agent_loop import config, models


# --------------------------------------------------------------------------
# the schema can express more than one
# --------------------------------------------------------------------------
def test_a_role_defaults_to_a_single_member():
    rs = config.RoleSettings(model="a:cloud", max_tokens=100, think=False)
    assert rs.all_members == ("a:cloud",)


def test_a_role_can_name_several_members():
    rs = config.RoleSettings(
        model="a:cloud", max_tokens=100, think=False,
        extra_members=("b:cloud",),
    )
    assert rs.all_members == ("a:cloud", "b:cloud")


def test_the_registry_gets_every_member_not_just_the_first():
    """`ModelRegistry.register` appends, and its docstring has always claimed the
    panel is several reviewers from different families. `registry_from_config`
    registered one, so the claim was unreachable from config."""
    cfg = config.load(None)
    reg = models.registry_from_config(cfg)
    assert len(reg.get_all("reviewer")) >= 2, [c.name for c in reg.get_all("reviewer")]


def test_single_member_roles_are_unaffected():
    cfg = config.load(None)
    reg = models.registry_from_config(cfg)
    for role in ("implementer", "arbiter", "compactor"):
        assert len(reg.get_all(role)) == 1, role


# --------------------------------------------------------------------------
# the shipped default satisfies the policy
# --------------------------------------------------------------------------
def test_the_default_panel_has_at_least_two_members():
    assert len(config.load(None).role("reviewer").all_members) >= 2


def test_the_default_panel_members_are_from_different_families():
    """"Preferably from different view points" is the whole justification for the
    panel's cost: two models from one family miss the same things."""
    fams = {models.model_family(m) for m in config.load(None).role("reviewer").all_members}
    assert len(fams) >= 2, f"all default reviewers are from one family: {fams}"


def test_every_default_reviewer_is_in_the_catalogue():
    """The catalogue is what records what a model IS. A default naming a model it
    does not describe is a default nobody can reason about."""
    for m in config.load(None).role("reviewer").all_members:
        assert m in config.MODEL_CATALOG, m


# --------------------------------------------------------------------------
# family detection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,family", [
    ("glm-5.2:cloud", "glm"),
    ("minimax-m3:cloud", "minimax"),
    ("kimi-k2.7-code:cloud", "kimi"),
    ("mistral-large-3:675b-cloud", "mistral"),
    ("qwen3.5:cloud", "qwen"),
    ("gemma4:31b-cloud", "gemma"),
    ("claude-opus-5", "claude"),
    ("agy:claude-opus-4-6-thinking", "claude"),
    ("gemini-3.6-flash", "gemini"),
    ("deepseek-v4-pro:cloud", "deepseek"),
])
def test_model_family_reads_the_vendor_stem(name, family):
    assert models.model_family(name) == family


def test_a_backend_prefix_is_not_the_family():
    """`agy:` is a transport, not a viewpoint. Two agy-routed Claudes are one
    family, and treating the prefix as the family would call them two."""
    assert models.model_family("agy:claude-sonnet-4-6") == models.model_family("claude-sonnet-5")


# --------------------------------------------------------------------------
# the guard: a future edit cannot silently drop back to one
# --------------------------------------------------------------------------
def test_a_one_member_default_panel_is_refused_by_the_config_guard():
    bad = dict(config._DEFAULT_ROLES)
    bad["reviewer"] = config.RoleSettings(
        model="glm-5.2:cloud", max_tokens=24000, think=False, extra_members=(),
    )
    with pytest.raises(ValueError, match="at least two"):
        config.check_panel_policy(bad)


def test_a_same_family_default_panel_is_refused_by_the_config_guard():
    bad = dict(config._DEFAULT_ROLES)
    bad["reviewer"] = config.RoleSettings(
        # Two DIFFERENT models of one family. The same name twice dedupes to a
        # single member and would hit the "at least two" guard instead, testing
        # the wrong branch.
        model="glm-5.2:cloud", max_tokens=24000, think=False,
        extra_members=("glm-4.9:cloud",),
    )
    with pytest.raises(ValueError, match="famil"):
        config.check_panel_policy(bad)


def test_the_shipped_defaults_pass_their_own_guard():
    config.check_panel_policy(config._DEFAULT_ROLES)


# --------------------------------------------------------------------------
# and the CLI says so when a RUN violates the policy
# --------------------------------------------------------------------------
def test_the_cli_warns_when_two_reviewers_share_a_family(capsys):
    from unittest.mock import patch

    from agent_loop import cli
    from agent_loop.profiles import Profile, register

    register(Profile(
        name="test-o22-cli",
        language="python", file_suffixes=(".py",), line_comment="#",
        block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    ))
    # `plan_mode.chat` is stubbed: the first version of this test let the run make
    # a REAL model call to reach a warning printed before any call happens.
    from agent_loop import plan_mode
    from agent_loop.providers import Completion

    with patch.object(plan_mode, "chat", return_value=Completion(text="", model="m")):
        cli.main([
            "--mode", "plan", "--profile", "test-o22-cli", "--defect", "d",
            "--reviewers", "glm-5.2:cloud,glm-5.2:cloud", "--fast-plan",
            "--max-rounds", "1",
        ])
    out = capsys.readouterr().out
    assert "same family" in out.lower() or "one viewpoint" in out.lower(), out
