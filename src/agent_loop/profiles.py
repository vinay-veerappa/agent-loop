"""
profiles.py
===========
Language-agnostic profile interface. The loop driver, gates, and region
extractor contain zero language-specific strings -- everything lives in a
Profile and is injected at call time.

A consumer (e.g. tvDownloadOHLC) creates profile instances and registers
them. The package ships only the interface and a default-protected-paths
fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Sequence, Tuple


_OUTPUT_CONTRACT = """
OUTPUT FORMAT - obey exactly, no prose outside the blocks:
For every region you were given, emit one block, even if unchanged:

<<<BLOCK id="REGION_ID">>>
...the complete replacement text for that region, first line to last line...
<<<END id="REGION_ID">>>

After all blocks, emit exactly one:
<<<NOTES>>>
- bullet list: what changed per region and why, plus any new config keys or fields you added
<<<END NOTES>>>
"""

_REVIEW_CONTRACT = """
OUTPUT FORMAT - obey exactly:
<<<VERDICT>>>
APPROVE | REVISE | REJECT
<<<END VERDICT>>>
<<<FINDINGS>>>
- [BLOCKER|MAJOR|MINOR] region_id: what is wrong, quoting the line, and the concrete failure case
(write "- NONE" if you found nothing at that severity)
<<<END FINDINGS>>>
<<<REQUIRED>>>
- imperative instructions the implementer must apply verbatim to reach APPROVE
(write "- NONE" if APPROVE)
<<<END REQUIRED>>>
"""


@dataclass
class Profile:
    """Everything language-specific and domain-specific the loop needs.

    The loop driver, gates, and region extractor read these fields and never
    hardcode a language, a lock name, or a build command.
    """
    name: str
    # Language
    language: str                          # "csharp", "python", "typescript", "go"
    file_suffixes: Tuple[str, ...]         # (".cs",) or (".py",) or (".ts", ".tsx")
    preprocessor_directives: Tuple[str, ...] = ()  # ("#if", "#endif") for C#; () for Python
    block_comment: Tuple[str, ...] = ("/*", "*/")  # for strip_code; ("#",) for Python
    line_comment: str = "//"               # "//" for C#/TS; "#" for Python/Go
    block_kind: str = "decl"               # "decl" for brace-delimited; "indent" for Python
    # Build and test
    build_cmd: str = ""
    test_cmd: str = ""
    test_runner_regex: Tuple[str, ...] = field(default_factory=tuple)
    # Lock-scope gate (optional; only for languages with a lock primitive)
    lock_name: str = ""                    # "_stateLock" for NT8; "" for Python (gate skipped)
    lock_pattern: str = ""                 # compiled per profile from lock_name
    risk_calls: Tuple[str, ...] = ()       # (".Flatten", ".Cancel", ...) for NT8; () for Python
    # File-level scope gate (Developer mode)
    file_scope_whitelist: Tuple[str, ...] = ()  # ("scripts/ninjatrader/addons/",) for nt8
    # Protected paths and test sources
    protected: Tuple[str, ...] = ()
    test_sources: Tuple[str, ...] = ()
    # Context injection budget (Phase 3 passive retrieval)
    context_token_budget: int = 3000
    # Per-round input budget (token efficiency)
    round_input_token_budget: int = 40000
    # Graph project name for codebase-memory-mcp (Phase 2/3)
    # When set, the loop checks graph freshness at startup and re-indexes if stale.
    # Format: the project name as registered in codebase-memory-mcp (e.g. "C-Users-vinay-agent-loop")
    graph_project: str = ""
    # Prompts and settled decisions
    implementer_rules: str = ""
    reviewer_priorities: str = ""
    settled: Tuple[str, ...] = ()

    @property
    def implementer_system(self) -> str:
        return self.implementer_rules.rstrip() + "\n" + _OUTPUT_CONTRACT

    @property
    def reviewer_system(self) -> str:
        return self.reviewer_priorities.rstrip() + "\n" + _REVIEW_CONTRACT


# Default protected paths used when a profile does not specify its own.
# These protect the agent loop's own tests and config from being edited by
# the implementer (anti reward-hacking).
DEFAULT_PROTECTED: Tuple[str, ...] = (
    "*Tests.cs",
    "*Tests.py",
    "test_*.py",
    "*Test.ts",
    "*_test.go",
    "*.csproj",
    "*.pyproject.toml",
    "agent_loop/*",
    "logs/agent_loop/*baseline*",
)


# Registry of profiles. Consumers register their profiles here.
# Example: PROFILES["nt8-riskguard"] = NT8_RISKGUARD
PROFILES: Dict[str, Profile] = {}


def register(profile: Profile) -> None:
    """Register a profile so the CLI can find it by name."""
    PROFILES[profile.name] = profile


def get(name: str) -> Profile:
    if name not in PROFILES:
        raise KeyError(f"unknown profile {name!r}; have {sorted(PROFILES)}")
    return PROFILES[name]