"""
Every tunable number has exactly one definition, in config.py.

Before this, the same values lived as bare literals in seven modules and several
were duplicated: loop.py passed max_tokens=48000 while models.py declared
max_tokens=48000 for the same model, agreeing only by coincidence.
ModelConfig.max_tokens was read in exactly one place, so per-model budgets were
dead configuration -- and when the implementer exhausted its budget on reasoning,
the provider's own advice ("raise max_tokens") was unreachable without editing
the loop.

test_no_budget_literals_at_call_sites is the guard that keeps them from coming
back. The rest verify that overriding actually takes effect, and that a bad
override fails loudly rather than silently doing nothing.
"""
import json
import re
from pathlib import Path

import pytest

from agent_loop import config, models

SRC = Path(config.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolate_config():
    """Never leak an override into another test."""
    config.reset()
    models.reload_default_registry(config.DEFAULTS)
    yield
    config.reset()
    models.reload_default_registry(config.DEFAULTS)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
def test_no_budget_literals_at_call_sites():
    """No module may pass a hardcoded max_tokens to chat().

    config.py is the only place a budget literal belongs. selftest.py is exempt:
    it drives run_ticket directly with deliberately tiny limits to force
    specific verdicts, which is a test fixture, not a tunable.
    """
    pattern = re.compile(r"max_tokens\s*=\s*\d+")
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name in ("config.py", "selftest.py"):
            continue
        text = py.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{py.relative_to(SRC)}:{line} -> {m.group(0)}")
    assert not offenders, (
        "hardcoded token budget(s) outside config.py; move the value into "
        "config.py and read it from there: " + "; ".join(offenders)
    )


def test_no_panel_deadline_literals_at_call_sites():
    """Same rule for the panel wall clock, which had three copies of 1800."""
    pattern = re.compile(r"deadline_secs\s*=\s*\d+")
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name in ("config.py", "selftest.py"):
            continue
        text = py.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            # A signature default of 0 means "ask config" and is the pattern we want.
            if m.group(0).endswith("= 0") or m.group(0).endswith("=0"):
                continue
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{py.relative_to(SRC)}:{line} -> {m.group(0)}")
    assert not offenders, "hardcoded panel deadline(s): " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# Defaults are coherent
# ---------------------------------------------------------------------------
def test_thinking_roles_have_budgets_that_cover_reasoning():
    """A think=True entry needs room for reasoning AND the answer.

    48000 with thinking on is exactly what produced IMPLEMENTER_UNREACHABLE
    (125,070 chars of reasoning, empty content), so nothing that thinks may be
    configured at or below it.
    """
    cfg = config.DEFAULTS
    for name, role in cfg.roles.items():
        if role.think:
            assert role.max_tokens > 48000, (
                f"role {name!r} thinks but has only {role.max_tokens} tokens; "
                "48000 with thinking on is the configuration that failed"
            )
    for name, mode in cfg.modes.items():
        if mode.think:
            assert mode.max_tokens >= 48000, (
                f"mode {name!r} thinks but has only {mode.max_tokens} tokens"
            )


def test_every_mode_declares_think_explicitly():
    """think must be a decision, not a model default.

    chat(think=None) omits the field, which leaves the model's own default in
    force -- ON for a reasoning model. Every mode used to do that while carrying
    a budget sized for output alone.
    """
    for name, mode in config.DEFAULTS.modes.items():
        assert isinstance(mode.think, bool), name


def test_registry_is_built_from_config_not_duplicated():
    reg = models.registry_from_config(config.DEFAULTS)
    for role, rs in config.DEFAULTS.roles.items():
        got = reg.get(role)
        assert got.name == rs.model
        assert got.max_tokens == rs.max_tokens
        assert got.think == rs.think


# ---------------------------------------------------------------------------
# Overriding works
# ---------------------------------------------------------------------------
def test_override_changes_a_role_budget_and_leaves_the_rest(tmp_path):
    cfg_file = tmp_path / "agent_loop.config.json"
    cfg_file.write_text(json.dumps({
        "roles": {"implementer": {"max_tokens": 120000}}
    }), encoding="utf-8")

    cfg = config.load(str(cfg_file))
    assert cfg.role("implementer").max_tokens == 120000
    # untouched fields inherit
    assert cfg.role("implementer").model == config.DEFAULTS.role("implementer").model
    assert cfg.role("implementer").think is True
    # untouched sections inherit
    assert cfg.role("reviewer") == config.DEFAULTS.role("reviewer")
    assert cfg.loop == config.DEFAULTS.loop


def test_override_can_change_the_model_for_a_role(tmp_path):
    """The knob the operator most wants: which model does which job."""
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "roles": {"reviewer": {"model": "kimi-k3:cloud"}}
    }), encoding="utf-8")
    cfg = config.load(str(cfg_file))
    assert cfg.role("reviewer").model == "kimi-k3:cloud"
    assert cfg.role("reviewer").max_tokens == config.DEFAULTS.role("reviewer").max_tokens

    # ...and it reaches the registry the loop reads.
    models.reload_default_registry(cfg)
    assert models.DEFAULT_REGISTRY.get("reviewer").name == "kimi-k3:cloud"


def test_override_reaches_the_budget_lookup_the_loop_uses(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "roles": {"implementer": {"max_tokens": 111000}}
    }), encoding="utf-8")
    models.reload_default_registry(config.load(str(cfg_file)))
    got = models.DEFAULT_REGISTRY.max_tokens_for("kimi-k2.7-code:cloud", "implementer", 1)
    assert got == 111000


def test_override_loop_and_provider_sections(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "loop": {"max_rounds": 7, "panel_deadline_secs": 60},
        "provider": {"temperature": 0.0},
    }), encoding="utf-8")
    cfg = config.load(str(cfg_file))
    assert cfg.loop.max_rounds == 7
    assert cfg.loop.panel_deadline_secs == 60
    assert cfg.provider.temperature == 0.0
    assert cfg.provider.num_ctx == config.DEFAULTS.provider.num_ctx


def test_a_new_role_may_be_added_but_must_be_complete(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "roles": {"planner": {"model": "m", "max_tokens": 1000, "think": False}}
    }), encoding="utf-8")
    cfg = config.load(str(cfg_file))
    assert cfg.role("planner").model == "m"

    cfg_file.write_text(json.dumps({"roles": {"planner": {"model": "m"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="must define"):
        config.load(str(cfg_file))


# ---------------------------------------------------------------------------
# Bad config fails loudly
# ---------------------------------------------------------------------------
def test_unknown_key_is_rejected(tmp_path):
    """A silently ignored typo is worse than no config file at all."""
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "roles": {"implementer": {"max_token": 120000}}  # missing the 's'
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key"):
        config.load(str(cfg_file))


def test_unknown_section_is_rejected(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"lop": {"max_rounds": 2}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config section"):
        config.load(str(cfg_file))


def test_shipped_example_file_loads_and_equals_the_defaults():
    """The example is documentation, and documentation that drifts is a lie.

    It must load as-is (including its "_comment") and describe exactly what the
    code does, so an operator reading it learns the real current values.
    """
    example = Path(config.__file__).resolve().parent.parent.parent / "agent_loop.config.example.json"
    assert example.is_file(), example
    cfg = config.load(str(example))
    assert cfg == config.DEFAULTS, (
        "agent_loop.config.example.json no longer matches config.DEFAULTS; "
        "update the example so it documents the real values"
    )


def test_underscore_keys_are_treated_as_comments(tmp_path):
    """JSON has no comments, so "_note" is the convention -- at every level."""
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({
        "_comment": "ignored",
        "loop": {"_why": "ignored too", "max_rounds": 5},
    }), encoding="utf-8")
    assert config.load(str(cfg_file)).loop.max_rounds == 5


def test_malformed_json_is_reported_with_the_path(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        config.load(str(cfg_file))


def test_explicit_missing_path_is_an_error_not_a_silent_default():
    with pytest.raises(FileNotFoundError):
        config.load(str(Path("no") / "such" / "config.json"))


def test_env_var_pointing_at_a_missing_file_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError, match=config.CONFIG_ENV_VAR):
        config.load()


def test_no_config_file_anywhere_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    assert config.load(start=tmp_path) == config.DEFAULTS


def test_cwd_config_file_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    (tmp_path / config.DEFAULT_CONFIG_FILENAME).write_text(
        json.dumps({"loop": {"max_rounds": 9}}), encoding="utf-8"
    )
    assert config.load(start=tmp_path).loop.max_rounds == 9


def test_cli_reports_a_bad_config_instead_of_crashing(tmp_path, monkeypatch):
    from agent_loop import cli
    from agent_loop.profiles import Profile, register

    register(Profile(
        name="test-config-cli", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    ))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nope": {}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--mode", "report", "--profile", "test-config-cli",
                     "--config", str(bad)])
    assert code == 2
