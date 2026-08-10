"""
models.py
=========
Model-by-capability registry. Maps each role to a model based on capability
and cost. The arbiter must not be the same model as any reviewer. The
compactor uses a cheap model, never the implementer or arbiter.

Consumers can override per-ticket via CLI, but the registry validates the
override.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ModelConfig:
    name: str                # "kimi-k2.7-code:cloud"
    role: str                # "implementer", "reviewer", "arbiter", "compactor", "planner", "explorer", "tester"
    capability: str          # "strong-coder", "strong-reasoner", "cheap", "fast"
    cost_per_1m_out: float   # USD per 1M output tokens; 0.0 for subscription models
    think: bool = False      # whether chain-of-thought is on by default for this role
    max_tokens: int = 24000  # default output budget for this role
    cost_per_1m_in: float = 0.0  # USD per 1M input tokens


def family(model: str) -> str:
    """The model family, with backend prefix and version suffix removed.

    'ollama:glm-5.2:cloud' -> 'glm'. Used to keep the arbiter out of the
    panel's family: "not the same string" is too weak a test, because
    glm-5.1 arbitrating glm-5.2's findings inherits the same blind spots.
    """
    if model.startswith(("ollama:", "anthropic:", "openai:")):
        model = model.split(":", 1)[1]
    bare = model.split(":", 1)[0]
    # A version token ends the family name: glm-5.2 -> glm,
    # claude-opus-5 -> claude-opus, kimi-k2.7-code -> kimi.
    parts: List[str] = []
    for token in bare.split("-"):
        if any(ch.isdigit() for ch in token):
            break
        parts.append(token)
    return "-".join(parts) or bare


class ModelRegistry:
    """Declarative mapping from role to model, with validation rules.

    A role may hold more than one model. The panel is deliberately several
    reviewers from different families, so `register` APPENDS for a role that
    is already occupied; overwriting silently reduced the panel to one member.
    """

    def __init__(self) -> None:
        self._configs: Dict[str, List[ModelConfig]] = {}

    def register(self, config: ModelConfig) -> None:
        self._configs.setdefault(config.role, []).append(config)

    def get(self, role: str) -> ModelConfig:
        """The first model registered for a role."""
        return self.get_all(role)[0]

    def get_all(self, role: str) -> List[ModelConfig]:
        if not self._configs.get(role):
            raise KeyError(f"no model registered for role {role!r}; have {sorted(self._configs)}")
        return list(self._configs[role])

    def validate(self, implementer: str, reviewers: List[str], arbiter: str) -> None:
        """Enforce the design rules. Raises ValueError on violation."""
        # Rule 1: the arbiter must not come from the same family as any reviewer.
        arbiter_family = family(arbiter)
        for r in reviewers:
            if family(r) == arbiter_family:
                raise ValueError(
                    f"arbiter model {arbiter!r} is from the same family as reviewer {r!r} "
                    f"(both {arbiter_family!r}). The arbiter must be from a different family "
                    f"than the panel -- a shared family means the arbiter inherits the "
                    f"reviewer's blind spots."
                )

    def cost_summary(self, role: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate the cost of a call. Returns 0.0 for subscription models."""
        config = self.get(role)
        return (
            input_tokens * config.cost_per_1m_in + output_tokens * config.cost_per_1m_out
        ) / 1e6


# Default registry. Consumers override by registering their own configs.
DEFAULT_REGISTRY = ModelRegistry()
DEFAULT_REGISTRY.register(ModelConfig("kimi-k2.7-code:cloud", "implementer", "strong-coder", 0.0, think=True, max_tokens=48000))
DEFAULT_REGISTRY.register(ModelConfig("glm-5.2:cloud", "reviewer", "fast", 0.0, think=False, max_tokens=24000))
DEFAULT_REGISTRY.register(ModelConfig("deepseek-v4-pro:cloud", "arbiter", "strong-reasoner", 0.0, think=False, max_tokens=24000))
DEFAULT_REGISTRY.register(ModelConfig("glm-5.2:cloud", "compactor", "cheap", 0.0, think=False, max_tokens=8000))