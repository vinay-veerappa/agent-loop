"""
O58 — the reviewer and arbiter budgets were dead configuration.

`ModelRegistry.max_tokens_for` exists for exactly one reason, and its own
docstring says so: *"ModelConfig.max_tokens was read in exactly one place
(compaction), while every other call site hardcoded a literal -- so the
registry's per-model budgets were dead configuration."* It was then wired into
the implementer, and the other two roles kept their literals:

    def review_panel(..., max_tokens: int = 24000, think: Optional[bool] = False)
    def adjudicate(..., max_tokens: int = 24000)

**No caller passes either.** All five `review_panel` call sites (loop, plan_mode,
replay, review_mode, developer/driver) take the default, and `loop.py` calls
`adjudicate` without a budget.

Found because raising `roles.reviewer.max_tokens` from 24000 to 48000 to fix O57
changed nothing at all: minimax-m3 died on the very next run at the same
`eval_count=24000`. The config knob had never been connected.

These tests drive the real functions with a MODIFIED config rather than
asserting against the shipped number. Two equal literals agreeing by coincidence
is the exact failure `arbiter.py` records for `think`: *"a literal think=False
while config.py ALSO declared think=False -- the two agreed only by coincidence,
which is the exact failure config.py was created to end."* Asserting `== 48000`
would pass on a hardcoded 48000.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent_loop import arbiter as arb
from agent_loop import config
from agent_loop import loop as loop_mod
from agent_loop import models
from agent_loop.providers import Completion

REVIEW_BODY = (
    "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>"
)
ARBITER_BODY = (
    "<<<RULINGS>>>\n- [REJECTED] #1: no\n<<<END RULINGS>>>\n"
    "<<<RECOMMENDATION>>>\nREVISE\n<<<END RECOMMENDATION>>>\n"
    "<<<RATIONALE>>>\nr\n<<<END RATIONALE>>>"
)

# Deliberately not 24000 and not 48000: a value no literal in the tree can be.
ODD_BUDGET = 31337


def _activate(cfg):
    """Both steps `cli.main` takes, in its order.

    `set_active` alone is not enough: the registry holds the per-model budgets
    and is built from config ONCE at import, so a test that only sets the config
    measures the stale registry rather than the wiring it means to test. cli.py
    does `config.set_active(config.load(...))` then
    `models.reload_default_registry()`, and a test that skips the second half is
    not driving the real sequence.
    """
    config.set_active(cfg)
    models.reload_default_registry()


@pytest.fixture
def odd_config():
    """A config whose role budgets are unmistakable if they arrive."""
    base = config.DEFAULTS
    roles = dict(base.roles)
    for role in ("reviewer", "arbiter"):
        roles[role] = dataclasses.replace(roles[role], max_tokens=ODD_BUDGET)
    _activate(dataclasses.replace(base, roles=roles))
    try:
        yield
    finally:
        config.reset()
        models.reload_default_registry(config.DEFAULTS)


def test_the_reviewer_budget_comes_from_config(odd_config, tmp_path, monkeypatch):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        return Completion(text=REVIEW_BODY, model=model)

    monkeypatch.setattr(loop_mod, "chat", fake_chat)
    loop_mod.review_panel(["glm-5.2:cloud"], "prompt", "system", tmp_path, 1)

    assert seen["max_tokens"] == ODD_BUDGET, (
        f"the reviewer was called with {seen['max_tokens']}, not the configured "
        f"{ODD_BUDGET} -- roles.reviewer.max_tokens is not connected"
    )


def test_the_reviewer_think_flag_comes_from_config(odd_config, tmp_path, monkeypatch):
    """`think` had the same shape as the budget: a literal default nobody passed.

    It happens to agree with config today, which is precisely why it needs a test
    that moves the config value."""
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["think"] = kw.get("think")
        return Completion(text=REVIEW_BODY, model=model)

    base = config.get()
    roles = dict(base.roles)
    roles["reviewer"] = dataclasses.replace(roles["reviewer"], think=True)
    _activate(dataclasses.replace(base, roles=roles))
    try:
        monkeypatch.setattr(loop_mod, "chat", fake_chat)
        loop_mod.review_panel(["glm-5.2:cloud"], "prompt", "system", tmp_path, 1)
    finally:
        config.reset()
        models.reload_default_registry(config.DEFAULTS)

    assert seen["think"] is True, (
        "the reviewer's think flag is a literal, not the configured value"
    )


def test_an_explicit_argument_still_wins_over_config(odd_config, tmp_path, monkeypatch):
    """The parameter is an override, not decoration: a caller that knows better
    -- a bench, a replay holding one variable fixed -- must still be able to set
    it."""
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        return Completion(text=REVIEW_BODY, model=model)

    monkeypatch.setattr(loop_mod, "chat", fake_chat)
    loop_mod.review_panel(
        ["glm-5.2:cloud"], "prompt", "system", tmp_path, 1, max_tokens=1234
    )
    assert seen["max_tokens"] == 1234


def test_the_arbiter_budget_comes_from_config(odd_config, monkeypatch):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["max_tokens"] = kw.get("max_tokens")
        return Completion(text=ARBITER_BODY, model=model)

    class F:
        model, severity, text, blocking = "m", "MAJOR", "a finding", True

    monkeypatch.setattr(arb, "chat", fake_chat)
    arb.adjudicate(
        "mistral-large-3:675b-cloud",
        {"id": "T", "title": "t", "defect": "d"},
        [F()], "gates ok", "diff",
    )

    assert seen["max_tokens"] == ODD_BUDGET, (
        f"the arbiter was called with {seen['max_tokens']}, not the configured "
        f"{ODD_BUDGET} -- roles.arbiter.max_tokens is not connected"
    )


def test_no_function_defaults_a_budget_to_a_literal():
    """The literal default is what made the registry value unreachable, and it
    came back in two more roles after being fixed once for the implementer.

    Read from the AST, not by grepping the text. A grep matches PROSE: the first
    version of this test failed on `arbiter.py`'s own docstring quoting the old
    signature it had just removed, which is a test reporting a defect in a
    sentence describing the fix."""
    import ast

    offenders = []
    for mod in (loop_mod, arb):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            named = a.args + a.kwonlyargs
            defaults = ([None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
                        + list(a.kw_defaults))
            for arg, default in zip(named, defaults):
                if arg.arg != "max_tokens" or default is None:
                    continue
                if isinstance(default, ast.Constant) and isinstance(default.value, int):
                    offenders.append(
                        f"{mod.__name__}.{node.name}(max_tokens={default.value})"
                    )

    assert not offenders, (
        "a role budget defaults to a literal instead of coming from config: "
        + ", ".join(offenders)
    )
