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
    cost_per_1m_out: float   # USD; 0.0 for subscription models
    think: bool = False      # whether chain-of-thought is on by default for this role
    max_tokens: int = 24000  # default output budget for this role


class ModelRegistry:
    """Declarative mapping from role to model, with validation rules."""

    def __init__(self) -> None:
        self._configs: Dict[str, ModelConfig] = {}

    def register(self, config: ModelConfig) -> None:
        self._configs[config.role] = config

    def get(self, role: str) -> ModelConfig:
        if role not in self._configs:
            raise KeyError(f"no model registered for role {role!r}; have {sorted(self._configs)}")
        return self._configs[role]

    def validate(self, implementer: str, reviewers: List[str], arbiter: str) -> None:
        """Enforce the design rules. Raises ValueError on violation."""
        # Rule 1: the arbiter must not be the same model as any reviewer.
        bare = lambda m: m.split(":", 1)[-1] if m.startswith(("ollama:", "anthropic:", "openai:")) else m
        arbiter_bare = bare(arbiter)
        for r in reviewers:
            if bare(r) == arbiter_bare:
                raise ValueError(
                    f"arbiter model {arbiter!r} is the same as reviewer {r!r}. "
                    f"The arbiter must be from a different family than the panel -- "
                    f"a shared family means the arbiter inherits the reviewer's blind spots."
                )

    def cost_summary(self, role: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate the cost of a call. Returns 0.0 for subscription models."""
        config = self.get(role)
        return (input_tokens * config.cost_per_1m_out + output_tokens * config.cost_per_1m_out) / 1e6


# Default registry. Consumers override by registering their own configs.
DEFAULT_REGISTRY = ModelRegistry()
DEFAULT_REGISTRY.register(ModelConfig("kimi-k2.7-code:cloud", "implementer", "strong-coder", 0.0, think=True, max_tokens=48000))
DEFAULT_REGISTRY.register(ModelConfig("glm-5.2:cloud", "reviewer", "fast", 0.0, think=False, max_tokens=24000))
DEFAULT_REGISTRY.register(ModelConfig("deepseek-v4-pro:cloud", "arbiter", "strong-reasoner", 0.0, think=False, max_tokens=24000))
DEFAULT_REGISTRY.register(ModelConfig("glm-5.2:cloud", "compactor", "cheap", 0.0, think=False, max_tokens=8000))