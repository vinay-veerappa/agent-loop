"""
O59 — one budget number had to serve the configured model AND any substitute.

The consumer capped its implementer at 64000 with this reason recorded:

    a budget is only valid against a specific model's output ceiling, and
    nothing in the config can express that relationship. The package default of
    96000 is correct for kimi-k2.7-code, but qwen3.5's ceiling is 65536 and it
    rejects the request outright:
      HTTP 400 {"error":"max_tokens (96000) exceeds model's maximum output
                tokens (65536) for model qwen3.5"}

So the number was chosen for the WEAKEST model that might be substituted in, and
the model actually configured then ran at the substitute's ceiling on every
round. CM2 round 1 died there: kimi-k2.7-code emitted 282935 characters of
thinking and empty content at `eval_count=64000`.

Both failures are the same missing fact — the catalogue records what a model can
READ (`context_tokens`) and never recorded what it can WRITE.

`max_output_tokens` is recorded ONLY where measured. An unmeasured ceiling stays
None and is not clamped: guessing one would trade a loud HTTP 400 for a silently
truncated answer, and this project has already paid twice for confident wrong
readings of an API it could not check (O24, O34).
"""
from __future__ import annotations

import dataclasses

import pytest

from agent_loop import config, models


def test_a_measured_ceiling_clamps_a_larger_budget():
    """qwen3.5 rejects 96000 with an HTTP 400. Clamping turns a dead run into a
    smaller one, which is what the operator wanted from a substitution."""
    base = config.DEFAULTS
    roles = dict(base.roles)
    roles["implementer"] = dataclasses.replace(
        roles["implementer"], model="qwen3.5:cloud", max_tokens=96000
    )
    config.set_active(dataclasses.replace(base, roles=roles))
    models.reload_default_registry()
    try:
        got = models.DEFAULT_REGISTRY.max_tokens_for("qwen3.5:cloud", "implementer", 0)
    finally:
        config.reset()
        models.reload_default_registry(config.DEFAULTS)

    ceiling = config.MODEL_CATALOG["qwen3.5:cloud"].max_output_tokens
    assert ceiling == 65536, "the measured ceiling is the one from the 400 response"
    assert got == ceiling, (
        f"a 96000 budget was passed through to a model that refuses it (got {got})"
    )


def test_a_budget_under_the_ceiling_is_untouched():
    got = models.DEFAULT_REGISTRY.max_tokens_for("qwen3.5:cloud", "implementer", 1000)
    assert got <= config.MODEL_CATALOG["qwen3.5:cloud"].max_output_tokens


def test_an_unmeasured_ceiling_does_not_clamp():
    """The configured model must not be silently cut down to a guess.

    kimi's ceiling is not known -- it accepted 96000 and it accepted 64000, and
    neither tells us where it stops. So it stays None and 96000 goes through."""
    assert config.MODEL_CATALOG["kimi-k2.7-code:cloud"].max_output_tokens is None
    base = config.DEFAULTS
    roles = dict(base.roles)
    roles["implementer"] = dataclasses.replace(roles["implementer"], max_tokens=96000)
    config.set_active(dataclasses.replace(base, roles=roles))
    models.reload_default_registry()
    try:
        got = models.DEFAULT_REGISTRY.max_tokens_for(
            "kimi-k2.7-code:cloud", "implementer", 0
        )
    finally:
        config.reset()
        models.reload_default_registry(config.DEFAULTS)
    assert got == 96000


@pytest.mark.parametrize("name", sorted(config.MODEL_CATALOG))
def test_a_recorded_ceiling_is_never_above_the_context_window(name):
    """A model cannot emit more than it can hold. A ceiling above the context
    window is a transcription error, not a capability."""
    prof = config.MODEL_CATALOG[name]
    if prof.max_output_tokens is None or prof.context_tokens == 0:
        pytest.skip("no measured ceiling, or context not published")
    assert prof.max_output_tokens <= prof.context_tokens
