"""
The arbiter's model and `think` come from config, and are MEASURED choices.

`adjudicate` hardcoded `think=False` while config.py also declared think=False
for the arbiter role. The two agreed only by coincidence -- the exact failure
config.py exists to end -- so changing the config flag did nothing.
"""
import dataclasses
from unittest.mock import patch

from agent_loop import arbiter, config, models
from agent_loop.providers import Completion


def _capture_think(monkey_cfg=None):
    seen = {}

    def fake_chat(model, messages, **kw):
        seen["think"] = kw.get("think")
        return Completion(
            text="<<<RULINGS>>>\n- UPHELD #1: x\n<<<END RULINGS>>>\n"
                 "<<<RECOMMENDATION>>>\nREVISE\n<<<END RECOMMENDATION>>>\n"
                 "<<<RATIONALE>>>\nr\n<<<END RATIONALE>>>",
            model=model,
        )

    class F:
        model, severity, text, blocking = "m", "BLOCKER", "a finding", True

    ticket = {"id": "T", "title": "t", "defect": "d"}
    try:
        if monkey_cfg is not None:
            config.set_active(monkey_cfg)
        with patch.object(arbiter, "chat", side_effect=fake_chat):
            arbiter.adjudicate("some-model", ticket, [F()], "gates ok", "diff")
    finally:
        if monkey_cfg is not None:
            config.reset()
    return seen["think"]


def test_arbiter_think_comes_from_config_not_a_literal():
    base = config.DEFAULTS
    roles = dict(base.roles)
    roles["arbiter"] = dataclasses.replace(roles["arbiter"], think=True)
    assert _capture_think(dataclasses.replace(base, roles=roles)) is True
    assert _capture_think() is False, "the measured default is think=False"


def test_arbiter_is_not_in_the_panel_family():
    """A shared family means the arbiter inherits the panel's blind spots."""
    reg = models.reload_default_registry(config.DEFAULTS)
    arb = reg.get("arbiter").name
    reviewers = [c.name for c in reg.get_all("reviewer")]
    reg.validate(reg.get("implementer").name, reviewers, arb)
    assert models.family(arb) not in {models.family(r) for r in reviewers}


def test_arbiter_default_is_the_measured_winner():
    """Pinned deliberately. The previous default ruled SHIP on a patch with five
    real defects, twice, and this one refused it twice. If you change the model,
    re-run tests/fixtures/arbiter_bench/run_bench.py and update the numbers in
    config.py -- do not change it on taste."""
    assert config.DEFAULTS.roles["arbiter"].model == "mistral-large-3:675b-cloud"
    assert config.DEFAULTS.roles["arbiter"].think is False
