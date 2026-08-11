"""
ModelConfig.max_tokens must actually reach the call that spends it.

The field existed and was read in exactly one place (compaction). Every other
call site hardcoded a literal, so per-model budgets were dead configuration:
loop.py passed max_tokens=48000 while models.py declared max_tokens=48000 for
the same model. When O1's first run died because the implementer spent its whole
budget on reasoning and emitted empty content, there was no way to raise it
without editing the loop.
"""
from agent_loop.models import DEFAULT_REGISTRY, ModelConfig, ModelRegistry


def test_exact_model_name_wins_over_role_default():
    """An --implementer override must not inherit another model's budget."""
    reg = ModelRegistry()
    reg.register(ModelConfig("model-a", "implementer", "strong-coder", 0.0, max_tokens=48000))
    reg.register(ModelConfig("model-b", "implementer", "strong-coder", 0.0, max_tokens=96000))
    assert reg.max_tokens_for("model-b", "implementer", 1) == 96000
    assert reg.max_tokens_for("model-a", "implementer", 1) == 48000


def test_unregistered_model_falls_back_to_the_role_default():
    reg = ModelRegistry()
    reg.register(ModelConfig("model-a", "implementer", "strong-coder", 0.0, max_tokens=48000))
    assert reg.max_tokens_for("some-model-nobody-registered", "implementer", 1) == 48000


def test_unknown_role_falls_back_to_the_caller_literal():
    """Must not raise: an unregistered role is not a reason to fail a run."""
    reg = ModelRegistry()
    assert reg.max_tokens_for("model-x", "no-such-role", 12345) == 12345


def test_default_implementer_budget_exceeds_the_one_that_failed():
    """Regression: 48000 was demonstrably too small for a two-region ticket."""
    budget = DEFAULT_REGISTRY.max_tokens_for("kimi-k2.7-code:cloud", "implementer", 0)
    assert budget > 48000, (
        "the implementer budget must exceed the value that produced "
        "IMPLEMENTER_UNREACHABLE on ticket O1"
    )


def test_loop_does_not_hardcode_the_implementer_budget():
    """The literal is what made the registry value unreachable."""
    from pathlib import Path
    import agent_loop.loop as loop_mod

    src = Path(loop_mod.__file__).read_text(encoding="utf-8")
    assert "max_tokens=48000" not in src, (
        "loop.py hardcodes the implementer budget again; read it from the "
        "registry via DEFAULT_REGISTRY.max_tokens_for()"
    )
    assert "max_tokens_for(" in src
