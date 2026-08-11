"""
selftest.py
===========
Offline end-to-end exercise of the loop, with the model calls stubbed.

    python -m agent_loop.selftest

A tool whose job is gating code on tests should not itself be untested. Model
responses are canned, so this costs nothing, is deterministic, and exercises
the parts that actually decide outcomes: the worktree, the baseline freeze, the
gate ladder, the panel's validity rules, and arbitration.

It does real work -- a real worktree, a real build, a real test run -- so it
takes a minute or two. It never touches the live tree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

from . import arbiter, loop, profiles, regions
from .providers import Completion, ProviderError

REPO = Path(__file__).resolve().parents[2]

# A minimal test profile for the selftest (Python).
#
# A hermetic test command: it reports one known failure and never touches a
# real suite, so the baseline is deterministic and every run is offline.
#
# It must exist at all. Without a test_cmd the whole `if profile.test_cmd:`
# block in run_ticket is skipped -- including the test-first refusal -- so the
# "expect_green naming a test that already passes -> refused" case below could
# never fire and asserted nothing for as long as it has existed.
_FAKE_TEST_CMD = (
    "python -c \"print('FAILED tests/selftest_fake.py::test_known_red'); "
    "print('==== 1 failed, 2 passed in 0.01s ====')\""
)

_SELFTEST_PROFILE = profiles.Profile(
    name="selftest",
    language="python", file_suffixes=(".py",),
    line_comment="#", block_comment=(), block_kind="indent",
    test_cmd=_FAKE_TEST_CMD,
    test_sources=("tests/acceptance/*.py",),
    implementer_rules="test", reviewer_priorities="test",
)

APPROVE_BODY = (
    "<<<VERDICT>>>\nAPPROVE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- NONE\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- NONE\n<<<END REQUIRED>>>"
)
REVISE_BODY = (
    "<<<VERDICT>>>\nREVISE\n<<<END VERDICT>>>\n"
    "<<<FINDINGS>>>\n- [BLOCKER] X: invented problem\n<<<END FINDINGS>>>\n"
    "<<<REQUIRED>>>\n- do something\n<<<END REQUIRED>>>"
)
# The same dissent below BLOCKER severity. Since session 6 an arbiter cannot
# recommend SHIP over a dismissed BLOCKER, so the scenario that exercises the
# SHIP path end-to-end needs a dissent that does not trip that rule -- and the
# scenario that exercises the new rule needs one that does. Both exist below.
REVISE_MINOR_BODY = REVISE_BODY.replace("[BLOCKER]", "[MINOR]")


def _identity_patch(ticket: Dict) -> str:
    """An implementer response that returns every region unchanged.

    Unchanged source must sail through every gate; if it does not, a gate is
    broken rather than the patch being bad.
    """
    regs = regions.extract(REPO, ticket["regions"], _SELFTEST_PROFILE)
    parts = [f'<<<BLOCK id="{r.id}">>>\n{r.text}\n<<<END id="{r.id}">>>' for r in regs]
    parts.append("<<<NOTES>>>\n- no change (selftest)\n<<<END NOTES>>>")
    return "\n".join(parts)


def _arbiter_body(n: int, verdict: str, rec: str) -> str:
    rulings = "\n".join(f"- [{verdict}] #{i}: canned ruling" for i in range(1, n + 1))
    return (
        f"<<<RULINGS>>>\n{rulings}\n<<<END RULINGS>>>\n"
        f"<<<RECOMMENDATION>>>\n{rec}\n<<<END RECOMMENDATION>>>\n"
        f"<<<RATIONALE>>>\ncanned\n<<<END RATIONALE>>>\n"
        f"<<<SETTLED>>>\n- NONE\n<<<END SETTLED>>>"
    )


def _stub(impl_text: str, reviewer_behaviour: Dict[str, str]):
    """Return a chat() replacement. reviewer_behaviour maps model -> canned body
    or the sentinel 'RAISE' / 'EMPTY'."""

    def fake_chat(model_spec, messages, **kw):
        if model_spec in reviewer_behaviour:
            b = reviewer_behaviour[model_spec]
            if b == "RAISE":
                raise ProviderError(f"{model_spec}: simulated HTTP 502")
            return Completion(text="" if b == "EMPTY" else b, model=model_spec, secs=0.1)
        return Completion(text=impl_text, model=model_spec, secs=0.1)

    return fake_chat


def scenario(
    name: str,
    ticket: Dict,
    reviewers: List[str],
    behaviour: Dict[str, str],
    expect: str,
    arbiter_model: str = "",
) -> bool:
    print(f"\n--- {name}")
    original_loop, original_arb = loop.chat, arbiter.chat
    stub = _stub(_identity_patch(ticket), behaviour)
    loop.chat = stub
    arbiter.chat = stub
    try:
        res = loop.run_ticket(
            REPO,
            ticket,
            _SELFTEST_PROFILE,
            "stub-implementer",
            reviewers,
            max_rounds=2,
            apply=False,
            arbiter_model=arbiter_model,
        )
    finally:
        loop.chat, arbiter.chat = original_loop, original_arb
    got = res.get("final_verdict")
    ok = got == expect
    print(f"    expect={expect}  got={got}  {'PASS' if ok else 'FAIL'}")
    return ok


# --------------------------------------------------------------------------
# Parser fixtures - real malformations seen from real models
# --------------------------------------------------------------------------
# The canned bodies above are PERFECTLY formatted, which is exactly why they
# proved nothing: all four parser defects found while landing T2-T5 passed this
# selftest 8/8 while broken. A parser is only interesting on input a model
# actually produced, so these are verbatim shapes from logs/agent_loop (the
# artifacts themselves are gitignored, hence literals). Add one whenever a new
# malformation shows up in the wild.
_FIXTURES = [
    # T3 r2/r3/r4: block closed with '>>' instead of '>>>'. Cost 3 rounds and
    # the ticket; the static gate reported the block as "missing".
    (
        "block closed with >> (T3)",
        lambda: len(loop.parse_blocks(
            '<<<BLOCK id="A">>>\nbody a\n<<<END id="A">>\n'
            '<<<BLOCK id="B">>>\nbody b\n<<<END id="B">>>\n'
            "<<<NOTES>>\n- n\n<<<END NOTES>>"
        )[0]),
        2,
    ),
    (
        "notes closed with >> (T3)",
        lambda: len(loop.parse_blocks(
            '<<<BLOCK id="A">>>\nbody a\n<<<END id="A">>>\n<<<NOTES>>>\n- note\n<<<END NOTES>>'
        )[1]),
        6,  # len("- note")
    ),
    # T2 r1/r2: RATIONALE closed with <<<END SETTLED>>>, and no <<<SETTLED>>>
    # opener at all. Both sections parsed empty and 11 settled decisions were
    # silently discarded.
    (
        "rationale closed with the wrong END tag (T2)",
        lambda: arbiter._section(
            "<<<RATIONALE>>>\nthe reason\n<<<END SETTLED>>>\n- a settled item\n<<<END SETTLED>>>",
            "RATIONALE",
        ),
        "the reason",
    ),
    (
        "settled section with no opener (T2)",
        lambda: arbiter._section(
            "<<<RATIONALE>>>\nthe reason\n<<<END SETTLED>>>\n- a settled item\n<<<END SETTLED>>>",
            "SETTLED",
        ),
        "- a settled item",
    ),
    # T2 r1: stray bracket. T3 r2: no brackets at all -- eight valid rulings
    # parsed as unruled and turned a SHIP into a spurious ESCALATE.
    (
        "ruling bracket variants (T2, T3)",
        lambda: [
            (m.group(1), int(m.group(2)))
            for m in arbiter._RULING_RE.finditer(
                "- [REJECTED] #1: normal\n"
                "- [ [REJECTED] #2: stray bracket\n"
                "- REJECTED #3: no brackets\n"
                "- **[UPHELD]** #4: emphasised\n"
                "- [OUT_OF_SCOPE] #5 no colon"
            )
        ],
        [("REJECTED", 1), ("REJECTED", 2), ("REJECTED", 3), ("UPHELD", 4), ("OUT_OF_SCOPE", 5)],
    ),
    # Well-formed input must still parse identically -- a tolerant parser that
    # broke the happy path would be worse than the strict one.
    (
        "well-formed arbiter body still parses",
        lambda: (
            len(list(arbiter._RULING_RE.finditer(arbiter._section(_arbiter_body(3, "UPHELD", "REVISE"), "RULINGS")))),
            arbiter._section(_arbiter_body(3, "UPHELD", "REVISE"), "RATIONALE"),
        ),
        (3, "canned"),
    ),
]


def parser_fixtures() -> bool:
    print("\n--- parser fixtures: real malformed model output")
    ok = True
    for name, fn, expect in _FIXTURES:
        try:
            got = fn()
        except Exception as exc:  # a parser must never raise on model output
            got = f"RAISED {exc!r}"
        good = got == expect
        ok = ok and good
        print(f"    {'PASS' if good else 'FAIL'}  {name}")
        if not good:
            print(f"          expect={expect!r}\n          got   ={got!r}")
    return ok


def main() -> int:
    # The selftest runs the real loop against THIS repo's own source: it extracts
    # regions from `src/agent_loop/` and reads a ticket from `tickets/`. Neither
    # is carried by the wheel, so under an installed package REPO resolves to
    # `<venv>/Lib` and the first read died with a bare FileNotFoundError pointing
    # at a path nobody wrote. Say what is actually wrong instead -- the consumer
    # venv is exactly where someone follows HANDOVER §5 and runs this first.
    spec_path = REPO / "tickets" / "phase1_state_machine.json"
    if not spec_path.exists():
        print(
            f"selftest needs an agent-loop source checkout, and this is not one.\n"
            f"  package  : {Path(__file__).resolve().parent}\n"
            f"  looked in: {REPO}\n"
            f"It exercises the loop against the repo's own source and tickets/,\n"
            f"neither of which ships in the wheel. Clone the repo and run it from\n"
            f"there; `pytest tests/` is the check that works against an install."
        )
        return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    t3 = spec["tickets"][0]  # use the first ticket in the file
    # Keep artifacts out of the real ticket directories.
    t3 = dict(t3, id="SELFTEST")

    # The state-machine scenarios below assert panel/arbiter transitions, so they
    # must not also be subject to the acceptance gate: the ticket file's
    # expect_green names a test that is green in the hermetic baseline, which
    # (correctly) refuses the ticket before any panel runs. The expect_green
    # property has its own two cases at the end of this function.
    t3_states = {k: v for k, v in t3.items() if k != "expect_green"}

    R = ["rev-a", "rev-b"]
    results = [
        scenario(
            "unchanged source + unanimous APPROVE -> approved",
            t3_states,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": APPROVE_BODY},
            "APPROVE",
        ),
        scenario(
            # ARBITER_NEVER_RAN, not MAX_ROUNDS_EXHAUSTED: this scenario passes
            # no arbiter model, and the loop distinguishes "the arbiter tried and
            # could not converge" from "the arbiter never had a chance". The
            # expectation here was left at the old name when that split landed.
            "one reviewer dissents, no arbiter -> rounds exhausted, nothing applied",
            t3_states,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY},
            "ARBITER_NEVER_RAN",
        ),
        scenario(
            "one reviewer returns EMPTY (the T2 bug) -> panel invalid, NOT a rejection",
            t3_states,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": "EMPTY"},
            "PANEL_UNREACHABLE",
        ),
        scenario(
            "both reviewers 502 (T2 round 4) -> panel invalid",
            t3_states,
            R,
            {"rev-a": "RAISE", "rev-b": "RAISE"},
            "PANEL_UNREACHABLE",
        ),
    ]

    ARB = "stub-arbiter"
    results += [
        scenario(
            "dissent + arbiter rejects every finding -> ARBITER_SHIP (human signs off)",
            t3_states, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_MINOR_BODY,
             ARB: _arbiter_body(1, "REJECTED", "SHIP")},
            "ARBITER_SHIP", arbiter_model=ARB,
        ),
        scenario(
            "arbiter rejects a BLOCKER and ships anyway -> ESCALATED, not ARBITER_SHIP",
            t3_states, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY,
             ARB: _arbiter_body(1, "REJECTED", "SHIP")},
            "ESCALATED", arbiter_model=ARB,
        ),
        scenario(
            "dissent + arbiter upholds -> keeps revising, never auto-ships",
            t3_states, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY, ARB: _arbiter_body(1, "UPHELD", "REVISE")},
            "MAX_ROUNDS_EXHAUSTED", arbiter_model=ARB,
        ),
        scenario(
            "arbiter says ESCALATE -> stops immediately, no further spend",
            t3_states, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY, ARB: _arbiter_body(1, "UPHELD", "ESCALATE")},
            "ESCALATED", arbiter_model=ARB,
        ),
    ]

    # A ticket aimed at the verifier must be refused before any model runs.
    evil = dict(t3_states, id="SELFTEST_EVIL", regions=[
        {"id": "X", "file": "scripts/ninjatrader/addons/RiskGuardAddOnTests.cs", "anchor": "class"}
    ])
    print("\n--- ticket targeting the test file -> refused before any model call")
    res = loop.run_ticket(REPO, evil, _SELFTEST_PROFILE, "x", ["y"], max_rounds=1)
    ok = res.get("final_verdict") == "TICKET_REJECTED"
    print(f"    expect=TICKET_REJECTED  got={res.get('final_verdict')}  {'PASS' if ok else 'FAIL'}")
    results.append(ok)

    # Test-first: a ticket naming an acceptance test that is NOT red at baseline
    # is refused. Guards the vacuous-gate case -- a typo'd name would otherwise
    # make expect_green silently unfalsifiable.
    print("\n--- expect_green naming a test that already passes -> refused")
    bad = dict(t3_states, id="SELFTEST_EXPECT", expect_green=["TestThatDoesNotExistAnywhere"])
    res = loop.run_ticket(REPO, bad, _SELFTEST_PROFILE, "x", ["y"], max_rounds=1)
    ok = res.get("final_verdict") == "TICKET_REJECTED"
    print(f"    expect=TICKET_REJECTED  got={res.get('final_verdict')}  {'PASS' if ok else 'FAIL'}")
    results.append(ok)

    # The other half of the same property: a name that IS red at baseline is
    # accepted, and then the acceptance gate can still fail. Asserting only the
    # refusal would pass even if the check refused everything.
    print("\n--- expect_green red at baseline -> accepted, and the gate can fail")
    results.append(scenario(
        "acceptance test stays red -> gate fails, never approves",
        dict(t3_states, id="SELFTEST_STILL_RED", expect_green=["test_known_red"]),
        R,
        {"rev-a": APPROVE_BODY, "rev-b": APPROVE_BODY},
        "ARBITER_NEVER_RAN",
    ))

    # And the extractor must return the DECLARATION, not a call site.
    print("\n--- acceptance-test extractor returns the declaration body")
    name = "test_p1_7_quorum_partial_panel"
    src = loop.extract_test_sources(
        REPO, [name], _SELFTEST_PROFILE.test_sources, _SELFTEST_PROFILE
    )
    ok = f"def {name}(" in src and "assert " in src
    print(f"    {'PASS' if ok else 'FAIL'}  {len(src)} chars, {src.count('assert ')} assertion(s)")
    results.append(ok)

    results.append(parser_fixtures())

    passed = sum(results)
    print(f"\n==== selftest: {passed}/{len(results)} passed ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

