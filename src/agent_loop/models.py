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

import re

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import config
# model_family lives in config.py, which needs it for check_panel_policy at
# module scope. Importing it back from here made `import agent_loop.models`
# a circular import whenever it was the FIRST import of the package (O52).
# Re-exported because cli.py and two tests already import it from models.
from .config import model_family, _BACKEND_PREFIXES  # noqa: F401


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

    def max_tokens_for(self, model: str, role: str, fallback: int) -> int:
        """The output budget for `model` acting in `role`.

        Prefers an exact model-name match, because `--implementer <other-model>`
        must not silently inherit the budget of whatever model happens to be
        registered first for the role. Falls back to the role's default, then to
        the caller's literal.

        This exists because ModelConfig.max_tokens was read in exactly one place
        (compaction), while every other call site hardcoded a literal -- so the
        registry's per-model budgets were dead configuration. loop.py hardcoded
        48000 while models.py declared 48000 for the same model, and when the
        implementer exhausted that budget on reasoning there was no way to raise
        it short of editing the loop.
        """
        for cfg in self._configs.get(role, []):
            if cfg.name == model:
                return cfg.max_tokens
        try:
            return self.get(role).max_tokens
        except KeyError:
            return fallback

    def cost_summary(self, role: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate the cost of a call. Returns 0.0 for subscription models."""
        config = self.get(role)
        return (
            input_tokens * config.cost_per_1m_in + output_tokens * config.cost_per_1m_out
        ) / 1e6


# Default registry, built FROM config.py rather than repeating it. These four
# entries were previously literals here, duplicating the literals at the call
# sites that spend them -- see config.py for why that went wrong and for the
# reason behind each number.
DEFAULT_REGISTRY = ModelRegistry()


# Backend prefixes. `agy:` is a TRANSPORT, not a viewpoint: two agy-routed Claudes
# are one family, and treating the prefix as the family would count them as two.
def registry_from_config(cfg: "config.Config") -> ModelRegistry:
    """A registry populated from a Config. One conversion, no duplicated values."""
    reg = ModelRegistry()
    for role, rs in cfg.roles.items():
        # EVERY member, not just `rs.model`. The registry appends per role and its
        # docstring always promised a multi-family panel; this loop registering
        # one config is what made that promise unreachable (O22).
        for member in rs.all_members:
            reg.register(
                ModelConfig(
                    member, role, rs.capability, rs.cost_per_1m_out,
                    think=rs.think, max_tokens=rs.max_tokens,
                    cost_per_1m_in=rs.cost_per_1m_in,
                )
            )
    return reg


def reload_default_registry(cfg: "Optional[config.Config]" = None) -> ModelRegistry:
    """Repopulate DEFAULT_REGISTRY in place from `cfg` (default: the active one).

    In place, because callers hold a reference to DEFAULT_REGISTRY; rebinding the
    module global would leave them reading a stale registry.
    """
    src = registry_from_config(cfg or config.get())
    DEFAULT_REGISTRY._configs = {  # noqa: SLF001 - same module family, by design
        role: list(cfgs) for role, cfgs in src._configs.items()
    }
    return DEFAULT_REGISTRY


reload_default_registry(config.DEFAULTS)