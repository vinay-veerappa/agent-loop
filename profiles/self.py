"""
Self-profile: lets the agent loop edit its own Python source.

This is the bootstrapping profile. Once it exists, the loop can run
tickets against src/agent_loop/ and patch itself.

Usage:
    agent-loop --profile agent-loop-self --profile-module profiles.self \
        --tickets tickets/phase1_state_machine.json --ticket P1-1
"""
from __future__ import annotations

from agent_loop.profiles import Profile, register

SELF = Profile(
    name="agent-loop-self",
    language="python",
    file_suffixes=(".py",),
    line_comment="#",
    block_comment=(),  # Python has no block comments; # is a line comment
    block_kind="indent",
    preprocessor_directives=(),  # Python has no preprocessor
    # Build and test
    build_cmd="python -m py_compile src/agent_loop/loop.py src/agent_loop/cli.py src/agent_loop/gates.py src/agent_loop/regions.py src/agent_loop/profiles.py src/agent_loop/models.py src/agent_loop/arbiter.py src/agent_loop/providers.py src/agent_loop/workspace.py",
    test_cmd="python -m pytest tests/ -v --tb=short 2>&1",
    # No lock primitive in Python — lock-scope gate is skipped
    lock_name="",
    risk_calls=(),
    # File-level scope gate (Developer mode): only edit src/agent_loop/
    file_scope_whitelist=("src/agent_loop/",),
    # Protected paths: tests and the profiles directory itself
    protected=(
        "test_*.py",
        "tests/*",
        "profiles/*",
        "tickets/*",
        "pyproject.toml",
    ),
    test_sources=("tests/acceptance/*.py",),
    # Context injection budget
    context_token_budget=3000,
    # Per-round input budget
    round_input_token_budget=40000,
    # Graph project for codebase-memory-mcp
    graph_project="C-Users-vinay-agent-loop",
    implementer_rules="""\
You are a senior Python engineer working on an AI agent loop package. You make
surgical, minimal, provably-correct edits to the agent_loop package.

HARD CONSTRAINTS (violating any of these fails review):
1. Target Python 3.10+. Use type hints. No backward-incompatible syntax for 3.10.
2. The file must compile and all existing tests must pass after your edit.
3. Do not rename existing public/internal members, do not change existing method
   signatures that callers depend on, and do not delete existing behaviour that
   is not part of the ticket.
4. Preserve the existing indentation style (4 spaces) and the exact leading
   indentation of the first line of each region you return.
5. Fail closed: if a safety precondition cannot be verified, take the conservative
   action (break the loop, return an error state), never the permissive one.
6. Do not weaken, delete, or work around a test in order to pass. If a test is
   wrong, say so in your notes and leave it alone -- you are not given access to
   test code.
7. The loop's state machine is critical. A state that lies (says MAX_ROUNDS when
   the arbiter never ran) is worse than no state at all. Be precise about which
   state the loop is in at every exit point.""",
    reviewer_priorities="""\
You are an adversarial code reviewer for the agent_loop package. You are
reviewing a proposed patch to the loop's own state machine. Assume the
implementer is confident and wrong. Your job is to find the case where this
patch makes the loop silently do the wrong thing.

Check, in priority order:
1. CORRECTNESS OF THE FIX: does it actually close the described defect, in every
   path? Does the new state fire in exactly the right condition, and not in any
   condition that was previously handled correctly?
2. STATE MACHINE INTEGRITY: does the new state or transition break any existing
   exit path? Can the loop now exit with a state that does not match what
   actually happened?
3. BACKWARD COMPATIBILITY: does the change break any existing caller that
   relies on the current state names or their meanings?
4. EDGE CASES: what happens on the first round? The last round? A resume? An
   unreachable panel? An unreachable arbiter? A ticket that targets protected
   paths?
5. TEST ADEQUACY: the acceptance tests are shown to you. Do they actually assert
   the thing that matters? Would they fail if the defect were reintroduced?
6. COMPILE BREAKS: Python 3.10+ compatibility, missing imports, wrong types.
7. REGRESSIONS: existing behaviour or existing tests that this would break.

Be specific. Cite the offending line text. Do not restate the ticket. Do not
praise.

The patch has already passed a compiler and the project's test suite, INCLUDING
every acceptance test listed for this ticket; a claim that it does not compile,
or that it fails a test, is therefore almost certainly wrong -- say so only with
a concrete mechanism. Gaps in what the tests COVER are still fair game.""",
    settled=(),
)

register(SELF)