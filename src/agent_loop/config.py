"""
config.py
=========
The single place every tunable number lives.

Why this module exists: the same values were previously spread across seven
modules as bare literals, and several were *duplicated* -- `loop.py` passed
`max_tokens=48000` while `models.py` declared `max_tokens=48000` for the same
model, and the two agreed only by coincidence. `ModelConfig.max_tokens` was read
in exactly one place in the whole package, so per-model budgets were dead
configuration. When the implementer exhausted its budget on reasoning and the
provider's own advice was "raise max_tokens", there was no way to do it without
editing the loop.

The rule now: **a tunable number appears as a literal exactly once, here, with
the reason it has that value.** Call sites read it. `tests/acceptance/
test_config_central.py` fails the build if a literal reappears at a call site.

Rationale lives in these comments rather than in the override file, because the
reason a number is what it is matters more than the number, and a JSON file
cannot hold a comment. Override by writing only the values you are changing to
`agent_loop.config.json` (or any path via `--config` / `$AGENT_LOOP_CONFIG`):

    {
      "roles":  {"implementer": {"model": "kimi-k3:cloud", "max_tokens": 120000}},
      "modes":  {"docs": {"max_tokens": 32000}},
      "loop":   {"max_rounds": 6}
    }

Unknown keys are a hard error, not a silent no-op: a typo in a config file that
is quietly ignored is worse than no config file, because the operator believes
the setting took effect.

--------------------------------------------------------------------------
A NOTE ON BUDGETS AND THINKING -- read before changing any max_tokens
--------------------------------------------------------------------------
On a reasoning model, **chain-of-thought is spent from the same budget as the
answer.** `providers.chat(think=None)` omits the field, which leaves the model's
own default in force -- and for a reasoning model that default is ON. So a
budget sized for the expected output silently becomes a budget shared with an
unbounded reasoning prefix.

This is not hypothetical. The main loop's implementer had 48000, which was
chosen as "plenty for a patch"; on a two-region ticket the model spent 125,070
characters on reasoning and returned EMPTY CONTENT, and the run died as
IMPLEMENTER_UNREACHABLE having produced nothing. Round 3 of the retry used
52,139 output tokens behind 222,413 characters of reasoning.

Therefore every role and mode below declares `think` EXPLICITLY, and any entry
with `think=True` carries a budget sized for reasoning plus answer, not answer
alone. If you turn thinking on for something, raise its budget in the same edit.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RoleSettings:
    """A model bound to a job, with the budget that job needs."""
    model: str
    max_tokens: int
    think: bool
    capability: str = ""
    cost_per_1m_out: float = 0.0
    cost_per_1m_in: float = 0.0


@dataclass(frozen=True)
class ModeSettings:
    """A non-patch mode's single generation call."""
    max_tokens: int
    think: bool
    # Developer mode only. TDD is the default, and it is a correctness
    # requirement rather than a style preference: without a test that fails
    # first, the gate ladder cannot refuse a fix for a defect the suite does
    # not already cover. O3's first developer-mode patch compiled, passed all
    # 232 tests, and did not fix the defect -- it read a dict key that does not
    # exist, so it was a no-op for every gate except one. Every gate was green
    # because nothing tested the thing being fixed.
    require_failing_test: bool = True


@dataclass(frozen=True)
class LoopSettings:
    max_rounds: int
    panel_deadline_secs: int
    context_token_budget: int
    round_input_token_budget: int
    compactor_input_token_budget: int = 48000


@dataclass(frozen=True)
class ProviderSettings:
    temperature: float
    timeout_secs: int
    num_ctx: int
    max_retries: int
    default_max_tokens: int


@dataclass(frozen=True)
class Config:
    roles: Mapping[str, RoleSettings]
    modes: Mapping[str, ModeSettings]
    loop: LoopSettings
    provider: ProviderSettings

    def role(self, name: str) -> RoleSettings:
        try:
            return self.roles[name]
        except KeyError:
            raise KeyError(
                f"no role {name!r} configured; have {sorted(self.roles)}"
            ) from None

    def mode(self, name: str) -> ModeSettings:
        try:
            return self.modes[name]
        except KeyError:
            raise KeyError(
                f"no mode {name!r} configured; have {sorted(self.modes)}"
            ) from None


# --------------------------------------------------------------------------
# Defaults -- the single source of truth
# --------------------------------------------------------------------------
_DEFAULT_ROLES: Dict[str, RoleSettings] = {
    # think=True is deliberate: the implementer is planning a patch, not
    # transcribing one. 96000 replaced 48000 after 48000 produced
    # IMPLEMENTER_UNREACHABLE on a two-region ticket (125,070 chars of reasoning,
    # empty content). The successful retry used 52,139 output tokens, so the
    # ceiling was genuinely the binding constraint rather than a symptom.
    "implementer": RoleSettings(
        model="kimi-k2.7-code:cloud", max_tokens=96000, think=True,
        capability="strong-coder",
    ),
    # think=False on purpose, and measured: on a T2-sized review, thinking ON
    # took 159s and burned the budget before emitting findings; thinking OFF
    # took 21s, 2.7k tokens, and returned ten findings. A reviewer enumerates
    # what it sees -- it does not need to plan.
    "reviewer": RoleSettings(
        model="glm-5.2:cloud", max_tokens=24000, think=False, capability="fast",
    ),
    # MEASURED, 2026-08-10, not assumed. See tests/fixtures/arbiter_bench:
    # glm-5.2 raised six findings on the O3 patch, five of them verified correct
    # by hand. Of the five, each arbiter upheld (n=2 runs each):
    #
    #   deepseek-v4-pro think=False   0/5  SHIP    <- the previous default
    #   deepseek-v4-pro think=True    0/5  SHIP
    #   glm-5.2         think=False   0/5, 1/5  REVISE
    #   glm-5.2         think=True    0/5  SHIP
    #   mistral-large-3 think=False   3/5, 3/5  REVISE
    #
    # deepseek reproduced its live failure exactly and deterministically: it
    # ruled SHIP on a patch with five real defects, twice. mistral-large-3 was
    # the only arm to refuse it, and did so identically on both runs.
    #
    # think=True did NOT help and is not a budget artifact -- both arms returned
    # complete, parseable recommendations at 64000. On glm it was strictly
    # WORSE, flipping REVISE to SHIP on both runs. Adjudication is not a task
    # that improves with more deliberation here; leave it off.
    #
    # 3/5 is an improvement, not a solution. mistral misses both findings about
    # TEST quality. Do not read a REVISE from it as a thorough review.
    "arbiter": RoleSettings(
        model="mistral-large-3:675b-cloud", max_tokens=24000, think=False,
        capability="strong-reasoner",
    ),
    # Summarisation only, and its output is bounded by construction.
    "compactor": RoleSettings(
        model="glm-5.2:cloud", max_tokens=8000, think=False, capability="cheap",
    ),
}

# Non-patch modes. Each of these used to be a bare literal at its call site with
# `think` left unset -- meaning thinking ON via the model default, sharing the
# budget. That is the 48000 failure in miniature, so each now says what it wants.
_DEFAULT_MODES: Dict[str, ModeSettings] = {
    # Generates a whole document (design docs and PRDs are the long ones). Was
    # 8000 with thinking implicitly on, which is the tightest ratio in the
    # package: a single observed changelog run already spent 2,386 chars of
    # reasoning against a 1,166-token answer. Thinking off, budget raised.
    "docs": ModeSettings(max_tokens=32000, think=False),
    # Enumerates candidate approaches with trade-offs. Breadth, not depth.
    "brainstorm": ModeSettings(max_tokens=16000, think=False),
    # Emits ticket JSON: regions, spec, expected-green tests. Thinking helps
    # here (it is deciding what the ticket IS), so the budget covers both.
    "plan": ModeSettings(max_tokens=48000, think=True),
    # Writes acceptance tests that must be RED for the right reason -- worth
    # reasoning about, hence the same treatment as plan.
    "test": ModeSettings(max_tokens=48000, think=True),
    # Multi-turn tool-calling loop; each turn is small, but it is choosing what
    # to do next, so thinking stays on and the per-turn budget covers it.
    "developer": ModeSettings(max_tokens=48000, think=True, require_failing_test=True),
}

_DEFAULT_LOOP = LoopSettings(
    # Four rounds. O1 exhausted three while oscillating between a capability and
    # a guarantee, which suggests raising this OR splitting the ticket; raising
    # it alone buys more attempts at the same confusion, so it stays at 4 until
    # there is evidence rather than a hunch.
    max_rounds=4,
    # Wall clock for the WHOLE panel, not per reviewer. A reviewer that has not
    # answered by then has not voted, and must not be counted as a dissent.
    panel_deadline_secs=1800,
    # Passive graph context injected into the implement prompt. The reviewer gets
    # half of this, so it can check "does this break callers?" without crowding
    # out the diff it is there to read.
    context_token_budget=3000,
    round_input_token_budget=40000,
    # How much prior-round text the COMPACTOR may read, in tokens. Phase 4b only
    # fires once the pruned history exceeds round_input_token_budget, so at the
    # moment this matters the input is at least 40000 tokens. It used to be a
    # hardcoded 20000 CHARACTERS -- about 5000 tokens -- with each message
    # separately cut to 2000 chars, so the compactor read roughly a tenth of the
    # rounds it was asked to summarise and the result was still labelled as the
    # summary of all of them. 48000 is comfortably above the 40000 trigger, so
    # the common case is summarised whole; when it still does not fit, the
    # coverage is now stated in the summary instead of being silently implied.
    compactor_input_token_budget=48000,
)

_DEFAULT_PROVIDER = ProviderSettings(
    # Not 0.0: greedy decoding makes a stuck model produce the same wrong patch
    # every round, which wastes the whole ladder. Low but non-zero.
    temperature=0.1,
    timeout_secs=900,
    # A floor, not a ceiling -- providers._fit_num_ctx grows it to fit
    # prompt+output. It was once SMALLER than the requested output budget, which
    # silently truncated every long generation.
    num_ctx=32768,
    max_retries=3,
    # Only for callers that name no budget. Every real call site should.
    default_max_tokens=16000,
)

DEFAULTS = Config(
    roles=dict(_DEFAULT_ROLES),
    modes=dict(_DEFAULT_MODES),
    loop=_DEFAULT_LOOP,
    provider=_DEFAULT_PROVIDER,
)


# --------------------------------------------------------------------------
# Loading and overriding
# --------------------------------------------------------------------------
CONFIG_ENV_VAR = "AGENT_LOOP_CONFIG"
DEFAULT_CONFIG_FILENAME = "agent_loop.config.json"


def _merge_dataclass(base: Any, overrides: Mapping[str, Any], where: str) -> Any:
    """Return a copy of `base` with `overrides` applied, rejecting unknown keys."""
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    known = {f.name for f in base.__dataclass_fields__.values()}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ValueError(
            f"unknown key(s) in {where}: {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(known))}"
        )
    return replace(base, **dict(overrides))


def merge(base: Config, overrides: Mapping[str, Any]) -> Config:
    """Apply a nested override mapping to a Config.

    Only the values present in `overrides` change; everything else is inherited
    from `base`. Unknown sections and unknown keys raise, because a silently
    ignored setting is indistinguishable from a working one.
    """
    # JSON has no comment syntax, so an underscore-prefixed key is the
    # convention for one. Accepted and ignored at every level -- otherwise
    # copying the shipped example file verbatim (which explains itself in a
    # "_comment") would fail with "unknown config section".
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}
    unknown_sections = sorted(set(overrides) - {"roles", "modes", "loop", "provider"})
    if unknown_sections:
        raise ValueError(
            f"unknown config section(s): {', '.join(unknown_sections)}. "
            "Known sections: loop, modes, provider, roles"
        )

    roles = dict(base.roles)
    for name, patch in (overrides.get("roles") or {}).items():
        if name not in roles:
            # A new role is legitimate (a consumer may add "planner"), but it has
            # to be complete -- a partial new role has no base to inherit from.
            required = {"model", "max_tokens", "think"}
            missing = sorted(required - set(patch))
            if missing:
                raise ValueError(
                    f"new role {name!r} must define {', '.join(sorted(required))}; "
                    f"missing {', '.join(missing)}"
                )
            roles[name] = RoleSettings(**dict(patch))
        else:
            roles[name] = _merge_dataclass(roles[name], patch, f"roles.{name}")

    modes = dict(base.modes)
    for name, patch in (overrides.get("modes") or {}).items():
        if name not in modes:
            required = {"max_tokens", "think"}
            missing = sorted(required - set(patch))
            if missing:
                raise ValueError(
                    f"new mode {name!r} must define {', '.join(sorted(required))}; "
                    f"missing {', '.join(missing)}"
                )
            modes[name] = ModeSettings(**dict(patch))
        else:
            modes[name] = _merge_dataclass(modes[name], patch, f"modes.{name}")

    loop_s = _merge_dataclass(base.loop, overrides.get("loop") or {}, "loop")
    prov_s = _merge_dataclass(base.provider, overrides.get("provider") or {}, "provider")
    return Config(roles=roles, modes=modes, loop=loop_s, provider=prov_s)


def find_config_file(explicit: Optional[str] = None, start: Optional[Path] = None) -> Optional[Path]:
    """Resolve which override file to use, in precedence order.

    explicit path > $AGENT_LOOP_CONFIG > ./agent_loop.config.json (if present).
    An explicit path or env var that does not exist is an ERROR rather than a
    silent fallback to defaults -- if you asked for a config, you need to know it
    was not found.
    """
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return p
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        p = Path(env)
        if not p.is_file():
            raise FileNotFoundError(
                f"{CONFIG_ENV_VAR} points at a file that does not exist: {env}"
            )
        return p
    candidate = (start or Path(".")) / DEFAULT_CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load(explicit: Optional[str] = None, start: Optional[Path] = None) -> Config:
    """Build the effective config: DEFAULTS, with any override file applied."""
    path = find_config_file(explicit, start)
    if path is None:
        return DEFAULTS
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object, got {type(raw).__name__}")
    return merge(DEFAULTS, raw)


# --------------------------------------------------------------------------
# Process-wide accessor
# --------------------------------------------------------------------------
_active: Optional[Config] = None


def get() -> Config:
    """The active config, loaded from disk on first use."""
    global _active
    if _active is None:
        _active = load()
    return _active


def set_active(cfg: Config) -> None:
    """Install a config. The CLI calls this once, after parsing --config."""
    global _active
    _active = cfg


def reset() -> None:
    """Forget the active config. For tests."""
    global _active
    _active = None
