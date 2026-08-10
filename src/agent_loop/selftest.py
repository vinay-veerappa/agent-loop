"""
selftest.py
===========
Offline end-to-end exercise of the loop, with the model calls stubbed.

    python -m scripts.agent_loop.selftest

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


def _identity_patch(ticket: Dict) -> str:
    """An implementer response that returns every region unchanged.

    Unchanged source must sail through every gate; if it does not, a gate is
    broken rather than the patch being bad.
    """
    regs = regions.extract(REPO, ticket["regions"])
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
            profiles.get("nt8-riskguard"),
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
    spec = json.loads((REPO / "scripts/agent_loop/tickets_p0.json").read_text(encoding="utf-8"))
    t3 = next(t for t in spec["tickets"] if t["id"] == "T3")
    # Keep artifacts out of the real ticket directories.
    t3 = dict(t3, id="SELFTEST")

    R = ["rev-a", "rev-b"]
    results = [
        scenario(
            "unchanged source + unanimous APPROVE -> approved",
            t3,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": APPROVE_BODY},
            "APPROVE",
        ),
        scenario(
            "one reviewer dissents -> rounds exhausted, nothing applied",
            t3,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY},
            "MAX_ROUNDS_EXHAUSTED",
        ),
        scenario(
            "one reviewer returns EMPTY (the T2 bug) -> panel invalid, NOT a rejection",
            t3,
            R,
            {"rev-a": APPROVE_BODY, "rev-b": "EMPTY"},
            "PANEL_UNREACHABLE",
        ),
        scenario(
            "both reviewers 502 (T2 round 4) -> panel invalid",
            t3,
            R,
            {"rev-a": "RAISE", "rev-b": "RAISE"},
            "PANEL_UNREACHABLE",
        ),
    ]

    ARB = "stub-arbiter"
    results += [
        scenario(
            "dissent + arbiter rejects every finding -> ARBITER_SHIP (human signs off)",
            t3, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY, ARB: _arbiter_body(1, "REJECTED", "SHIP")},
            "ARBITER_SHIP", arbiter_model=ARB,
        ),
        scenario(
            "dissent + arbiter upholds -> keeps revising, never auto-ships",
            t3, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY, ARB: _arbiter_body(1, "UPHELD", "REVISE")},
            "MAX_ROUNDS_EXHAUSTED", arbiter_model=ARB,
        ),
        scenario(
            "arbiter says ESCALATE -> stops immediately, no further spend",
            t3, R,
            {"rev-a": APPROVE_BODY, "rev-b": REVISE_BODY, ARB: _arbiter_body(1, "UPHELD", "ESCALATE")},
            "ESCALATED", arbiter_model=ARB,
        ),
    ]

    # A ticket aimed at the verifier must be refused before any model runs.
    evil = dict(t3, id="SELFTEST_EVIL", regions=[
        {"id": "X", "file": "scripts/ninjatrader/addons/RiskGuardAddOnTests.cs", "anchor": "class"}
    ])
    print("\n--- ticket targeting the test file -> refused before any model call")
    res = loop.run_ticket(REPO, evil, profiles.get("nt8-riskguard"), "x", ["y"], max_rounds=1)
    ok = res.get("final_verdict") == "TICKET_REJECTED"
    print(f"    expect=TICKET_REJECTED  got={res.get('final_verdict')}  {'PASS' if ok else 'FAIL'}")
    results.append(ok)

    # Test-first: a ticket naming an acceptance test that is NOT red at baseline
    # is refused. Guards the vacuous-gate case -- a typo'd name would otherwise
    # make expect_green silently unfalsifiable.
    print("\n--- expect_green naming a test that already passes -> refused")
    bad = dict(t3, id="SELFTEST_EXPECT", expect_green=["TestThatDoesNotExistAnywhere"])
    res = loop.run_ticket(REPO, bad, profiles.get("nt8-riskguard"), "x", ["y"], max_rounds=1)
    ok = res.get("final_verdict") == "TICKET_REJECTED"
    print(f"    expect=TICKET_REJECTED  got={res.get('final_verdict')}  {'PASS' if ok else 'FAIL'}")
    results.append(ok)

    # And the extractor must return the DECLARATION, not the call site in Main().
    print("\n--- acceptance-test extractor returns the method body")
    src = loop.extract_test_sources(
        REPO,
        ["TestCopyPath_LockedFollowerReceivesNoCopy"],
        profiles.get("nt8-riskguard").test_sources,
    )
    ok = "private static void TestCopyPath_LockedFollowerReceivesNoCopy" in src and "Assert(" in src
    print(f"    {'PASS' if ok else 'FAIL'}  {len(src)} chars, {src.count('Assert(')} assertion(s)")
    results.append(ok)

    results.append(parser_fixtures())

    passed = sum(results)
    print(f"\n==== selftest: {passed}/{len(results)} passed ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
